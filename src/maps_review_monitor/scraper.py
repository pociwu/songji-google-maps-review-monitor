from __future__ import annotations

from hashlib import sha256
import json
import logging
from pathlib import Path
import random
import re
import socket
import subprocess
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Playwright, sync_playwright

from .config import Settings, ShopConfig
from .models import OwnerReply, ProfileSummary, ReviewSnapshot

LOG = logging.getLogger(__name__)


class ScrapeError(RuntimeError):
    pass


def with_locale(url: str, locale: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["hl"] = locale
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def parse_star_label(label: str) -> float | None:
    match = re.search(r"([0-5](?:[.,]\d+)?)", label or "")
    return float(match.group(1).replace(",", ".")) if match else None


def parse_count(text: str, words: str) -> int | None:
    patterns = [rf"([\d,，.]+)\s*(?:{words})", rf"(?:{words})\s*([\d,，.]+)"]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(re.sub(r"\D", "", match.group(1)))
            except ValueError:
                pass
    return None


def merge_review_parts(parts: list[dict]) -> list[dict]:
    """Merge Google layouts that split reviewer header and review body."""
    merged: list[dict] = []
    by_id: dict[str, dict] = {}
    scalar_fields = (
        "author", "profile_url", "avatar_url", "stars_label", "time_text",
        "review_text", "reply_text", "reply_time",
    )
    for part in parts:
        review_id = str(part.get("id", "")).strip()
        if not review_id or review_id not in by_id:
            target = dict(part)
            target["photo_urls"] = list(part.get("photo_urls", []))
            merged.append(target)
            if review_id:
                by_id[review_id] = target
            continue
        target = by_id[review_id]
        for field in scalar_fields:
            if not target.get(field) and part.get(field):
                target[field] = part[field]
        target["photo_urls"] = list(dict.fromkeys([
            *target.get("photo_urls", []), *part.get("photo_urls", [])
        ]))
    return merged


class MapsScraper:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self._browser_process: subprocess.Popen[bytes] | None = None
        self.context: BrowserContext | None = None
        self._main_page: Page | None = None
        self._profile_visits = 0
        self.http = httpx.Client(
            timeout=45, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/140 Safari/537.36"},
        )

    def __enter__(self) -> "MapsScraper":
        self._pw = sync_playwright().start()
        self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        if self.settings.browser_executable:
            executable = Path(self.settings.browser_executable).expanduser()
        else:
            executable = Path(self._pw.chromium.executable_path)
        if not executable.exists():
            raise ScrapeError(f"找不到 Chromium 執行檔：{executable}")

        # Launch Chromium as an ordinary user process, then attach over CDP.
        # When Playwright launches Chromium itself, Google Maps can replace
        # reviews with its restricted view even with a signed-in profile.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        command = [
            str(executable),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.settings.browser_profile_dir.resolve()}",
            f"--lang={self.settings.locale}",
            "--window-size=1440,1100",
            "--no-first-run",
            "--disable-dev-shm-usage",
            "about:blank",
        ]
        if self.settings.headless:
            command.insert(-1, "--headless=new")
        self._browser_process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.settings.navigation_timeout_seconds
        while True:
            if self._browser_process.poll() is not None:
                raise ScrapeError(
                    "Chromium 啟動後立即結束；請確認專用設定檔未被其他 Chromium 使用"
                )
            try:
                response = self.http.get(f"{endpoint}/json/version", timeout=2)
                if response.is_success:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise ScrapeError("等待 Chromium 遠端除錯介面逾時")
            time.sleep(0.2)

        self.browser = self._pw.chromium.connect_over_cdp(endpoint)
        if not self.browser.contexts:
            raise ScrapeError("已連接 Chromium，但找不到預設瀏覽器設定檔")
        self.context = self.browser.contexts[0]
        self.context.add_init_script(
            "Object.defineProperty(Navigator.prototype, 'webdriver', "
            "{get: () => undefined});"
        )
        self.context.set_default_timeout(self.settings.navigation_timeout_seconds * 1000)
        # Reuse Chromium's naturally-created startup tab. Creating a new tab
        # through CDP is enough for some Google Maps sessions to be downgraded
        # to the restricted view even though the same profile works manually.
        self._main_page = self.context.pages[0] if self.context.pages else None
        if self._main_page is None:
            raise ScrapeError("Chromium 啟動後沒有可使用的原始分頁")
        self._main_page.set_viewport_size({"width": 1440, "height": 1100})
        if self.settings.timezone:
            session = self.context.new_cdp_session(self._main_page)
            session.send("Emulation.setTimezoneOverride", {"timezoneId": self.settings.timezone})
        return self

    def __exit__(self, *_: object) -> None:
        self.http.close()
        if self.browser:
            self.browser.close()
        if self._browser_process and self._browser_process.poll() is None:
            self._browser_process.terminate()
            try:
                self._browser_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._browser_process.kill()
        if self._pw:
            self._pw.stop()

    def scrape_shop(
        self, shop: ShopConfig,
        existing_lookup: Callable[[str], ReviewSnapshot | None] | None = None,
    ) -> list[ReviewSnapshot]:
        assert self.context is not None and self._main_page is not None
        page = self._main_page
        try:
            page.goto(with_locale(shop.url, self.settings.locale), wait_until="domcontentloaded")
            self._reject_block_page(page)
            feed = self._open_reviews(page)
            self._sort_newest(page)
            cards = self._load_cards(feed)
            if not cards:
                raise ScrapeError("找不到評論卡片；Google Maps 頁面結構可能已變更")
            # Extract every card before navigating away. Profile enrichment
            # then reuses the same natural Chromium tab instead of repeatedly
            # creating and closing automation-created tabs.
            extracted = []
            for card in cards:
                self._expand_card_text(card)
                extracted.append(self._extract_card(card))
            merged = merge_review_parts(extracted)
            complete_reviews = [
                raw for raw in merged
                if raw.get("stars_label") and (raw.get("author") or raw.get("profile_url"))
            ]
            raw_reviews = complete_reviews[: self.settings.scan_limit]
            skipped = len(merged) - len(complete_reviews)
            if skipped:
                LOG.warning("略過 %s 個不完整的非評論容器", skipped)
            if not raw_reviews:
                self._save_card_diagnostics(cards)
                raise ScrapeError("找到評論區塊，但沒有可辨識的完整評論卡片")
            result: list[ReviewSnapshot] = []
            for raw in raw_reviews:
                review = self._make_review(shop, raw)
                existing = existing_lookup(review.review_id) if existing_lookup else None
                if existing:
                    review.profile = existing.profile
                    if existing.photo_urls == review.photo_urls:
                        review.photo_paths = existing.photo_paths
                    else:
                        self._download_assets(review)
                else:
                    self._enrich_profile(review)
                    self._download_assets(review)
                result.append(review)
            return result
        except Exception:
            self.save_debug(page, shop.key)
            raise

    def _open_reviews(self, page: Page) -> Locator:
        feed = self._visible_review_feed(page)
        if feed is not None:
            return feed

        # Some signed-in layouts expose Overview/Reviews as tabs instead of a
        # "more reviews" button. Account language can override the hl query,
        # so support Traditional Chinese, English and Japanese labels.
        review_tabs = page.get_by_role(
            "tab", name=re.compile(r"^(?:評論|reviews?|クチコミ|口コミ)$", re.I)
        )
        for index in range(review_tabs.count()):
            tab = review_tabs.nth(index)
            if not tab.is_visible():
                continue
            tab.click()
            page.wait_for_timeout(1200)
            feed = self._visible_review_feed(page)
            if feed is not None:
                return feed
            cards = page.locator("div.jftiEf:visible, div.GHT2ce:visible")
            if cards.count():
                return self._review_container(cards)

        # Do not use a broad "評論|reviews" selector here: it also matches
        # "撰寫評論 / Write a review" and opens Google's sign-in dialog.
        # Prefer the explicit "更多評論 (N)" control. Clicking the rating's
        # "N 篇評論" link may only jump to the three-review preview section.
        open_buttons = page.locator(
            '[jsaction*="moreReviews"], button[aria-label^="更多評論"], '
            'button[aria-label^="More reviews" i], '
            'button[aria-label*="クチコミ"], button[aria-label*="口コミ"]'
        )
        clicked = False
        for _ in range(20):
            for index in range(open_buttons.count()):
                button = open_buttons.nth(index)
                if button.is_visible():
                    label = (button.get_attribute("aria-label") or button.inner_text()).strip()
                    if re.search(r"撰寫評論|write a review|クチコミを書く|口コミを書く", label, re.I):
                        continue
                    button.click()
                    clicked = True
                    break
            if clicked:
                break
            self._scroll_place_panel(page)
            self._page_random_wait(
                page,
                self.settings.scroll_delay_min_seconds,
                self.settings.scroll_delay_max_seconds,
            )
        # Very small listings may not render a "more reviews" button. In that
        # case the review-count control is an acceptable fallback.
        if not clicked:
            fallback_buttons = page.locator(
                'button[aria-label*="評論"], button[aria-label*="review" i], '
                'button[aria-label*="クチコミ"], button[aria-label*="口コミ"]'
            )
            for index in range(fallback_buttons.count()):
                button = fallback_buttons.nth(index)
                if button.is_visible():
                    label = (button.get_attribute("aria-label") or button.inner_text()).strip()
                    # A review-count control always contains a number. This
                    # deliberately excludes "撰寫評論 / Write a review".
                    if re.search(
                        r"\d[\d,，.]*.*(?:則評論|篇評論|reviews?|件のクチコミ|件の口コミ|クチコミ|口コミ)",
                        label,
                        re.I,
                    ):
                        button.click()
                        clicked = True
                        break
        # Listings with only a few reviews often have no "more reviews"
        # control at all. Their complete set is already rendered in the place
        # panel, so parse those cards directly.
        if not clicked:
            preview_cards = page.locator("div.jftiEf:visible")
            if preview_cards.count():
                return self._review_container(preview_cards)
        if not clicked:
            # The restricted-view notice is sometimes injected several
            # seconds after DOMContentLoaded. Re-check here so it is not
            # misreported as a missing reviews button.
            self._reject_block_page(page)
            raise ScrapeError("找不到「查看全部評論」按鈕；請確認網址是店家頁面")

        cards = page.locator("div.jftiEf:visible")
        try:
            cards.first.wait_for(state="visible", timeout=20000)
        except Exception:
            cards = page.locator("div.GHT2ce:visible")
            cards.first.wait_for(state="visible", timeout=20000)
        feed = self._visible_review_feed(page)
        if feed is not None:
            return feed
        return self._review_container(cards)

    @staticmethod
    def _review_container(cards: Locator) -> Locator:
        # Some Google Maps layouts omit role=feed. Use the nearest scrollable
        # place/review-panel ancestor of the first review card.
        container = cards.first.locator(
            "xpath=ancestor::div[@role='feed' or "
            "(contains(@class,'m6QErb') and "
            "(contains(@class,'DxyBCb') or contains(@class,'WNBkOb')))][1]"
        )
        if container.count():
            return container
        raise ScrapeError("評論已開啟，但找不到可捲動的評論容器")

    @staticmethod
    def _visible_review_feed(page: Page) -> Locator | None:
        feeds = page.locator('div[role="feed"]')
        for index in range(feeds.count()):
            feed = feeds.nth(index)
            if feed.is_visible():
                return feed
        return None

    @staticmethod
    def _scroll_place_panel(page: Page) -> None:
        panels = page.locator('div[role="main"], div.m6QErb.DxyBCb.kA9KIf.dS8AEf')
        for index in range(panels.count()):
            panel = panels.nth(index)
            if not panel.is_visible():
                continue
            moved = panel.evaluate("""node => {
              if (node.scrollHeight <= node.clientHeight + 20) return false;
              const before = node.scrollTop;
              node.scrollBy(0, Math.max(600, node.clientHeight * 0.8));
              return node.scrollTop !== before;
            }""")
            if moved:
                return

    def _sort_newest(self, page: Page) -> None:
        sort_buttons = page.locator(
            'button[aria-label*="排序"], button[aria-label*="Sort" i], '
            'button[aria-label*="並べ替え"], button[data-value="Sort"]'
        )
        if not sort_buttons.count():
            LOG.warning("找不到評論排序按鈕，將沿用頁面目前排序")
            return
        sort_buttons.first.click()
        choices = page.get_by_role(
            "menuitemradio", name=re.compile(r"最新|newest|新しい順", re.I)
        )
        try:
            choices.first.wait_for(state="visible", timeout=10000)
            choices.first.click()
        except Exception as exc:
            raise ScrapeError("排序選單已開啟，但找不到可點擊的「最新」選項") from exc
        page.wait_for_timeout(1200)

    def _load_cards(self, feed: Locator) -> list[Locator]:
        cards = self._review_cards(feed)
        first_class = cards.first.get_attribute("class") if cards.count() else ""
        # GHT2ce layouts use two sibling elements per logical review: reviewer
        # header followed by rating/content. Load twice as many DOM fragments.
        target = self.settings.scan_limit * 2 if "GHT2ce" in (first_class or "") else self.settings.scan_limit
        previous = -1
        unchanged = 0
        while cards.count() < target and unchanged < 4:
            count = cards.count()
            feed.evaluate("node => node.scrollTo(0, node.scrollHeight)")
            self._page_random_wait(
                feed.page,
                self.settings.scroll_delay_min_seconds,
                self.settings.scroll_delay_max_seconds,
            )
            if count == previous:
                unchanged += 1
            else:
                unchanged = 0
            previous = count
        return [cards.nth(i) for i in range(min(cards.count(), target))]

    @staticmethod
    def _review_cards(scope: Page | Locator) -> Locator:
        # data-review-id also appears on photo/like/share buttons. Restrict the
        # locator to the complete review-card elements.
        legacy = scope.locator("div.jftiEf:visible")
        if legacy.count():
            return legacy
        return scope.locator("div.GHT2ce:visible")

    @staticmethod
    def _expand_card_text(card: Locator) -> None:
        """Expand reviewer and owner-reply text without clicking controls outside the card."""
        selectors = (
            "button.w8nwRe",
            "button[jsaction*='expandReview']",
            "button[aria-label='全文']",
            "button[aria-label='更多']",
            "button[aria-label='More']",
        )
        try:
            buttons = card.locator(",".join(selectors))
            for index in range(min(buttons.count(), 4)):
                button = buttons.nth(index)
                if button.is_visible():
                    button.click(timeout=2000)
        except Exception as exc:
            LOG.debug("展開評論全文失敗，改以目前 DOM 內容擷取：%s", exc)

    def _extract_card(self, card: Locator) -> dict:
        return card.evaluate("""node => {
          const pick = sels => { for (const s of sels) { const e=node.querySelector(s); if(e) return e; } return null; };
          const text = sels => (pick(sels)?.innerText || pick(sels)?.textContent || '').trim();
          const attr = (sels, name) => pick(sels)?.getAttribute(name) || '';
          const urls = new Set();
          node.querySelectorAll('button[style*="background-image"], div[style*="background-image"]').forEach(e => {
            const m=(e.style.backgroundImage || '').match(/url\\(["']?(.*?)["']?\\)/); if(m) urls.add(m[1]);
          });
          node.querySelectorAll('img').forEach(e => { if(e.src && !e.closest('button[data-review-id]')) urls.add(e.src); });
          const avatar = attr(['button[data-href] img','a[href*="/contrib/"] img','.NBa7we'], 'src');
          if(avatar) urls.delete(avatar);
          const replyNode = pick(['.CDe7pd','.wiI7pd + div .MyEned','.d6SCIc']);
          const reviewIdNode = node.querySelector('[data-review-id]');
          return {
            id: node.getAttribute('data-review-id') || reviewIdNode?.getAttribute('data-review-id') || '',
            author: text(['.d4r55','.TSUbDb','button[aria-label] .fontHeadlineSmall']),
            profile_url: attr(['button[data-href]','a[href*="/contrib/"]'], 'data-href') || attr(['a[href*="/contrib/"]'], 'href'),
            avatar_url: avatar,
            stars_label: attr(['span[role="img"]'], 'aria-label'),
            time_text: text(['.rsqaWe','.dehysf']),
            review_text: text(['.wiI7pd','.MyEned']),
            photo_urls: [...urls].filter(x => x.includes('googleusercontent') || x.includes('ggpht')),
            reply_text: replyNode ? (replyNode.querySelector('.wiI7pd,.MyEned')?.innerText || replyNode.querySelector('.wiI7pd,.MyEned')?.textContent || replyNode.innerText || replyNode.textContent || '').trim() : '',
            reply_time: replyNode ? (replyNode.querySelector('.rsqaWe,.DZSIDd')?.textContent || '').trim() : ''
          };
        }""")

    def _make_review(self, shop: ShopConfig, raw: dict) -> ReviewSnapshot:
        review_id = raw.get("id", "").strip()
        if not review_id:
            seed = "\0".join([
                shop.key,
                raw.get("profile_url", ""),
                raw.get("author", ""),
                raw.get("stars_label", ""),
                raw.get("review_text", ""),
                "|".join(raw.get("photo_urls", [])),
            ])
            review_id = "fallback-" + sha256(seed.encode()).hexdigest()[:24]
            LOG.warning("評論缺少 Google 識別碼，使用替代識別：%s", review_id)
        reply = None
        if raw.get("reply_text"):
            reply = OwnerReply(text=raw["reply_text"], time_text=raw.get("reply_time", ""))
        return ReviewSnapshot(
            review_id=review_id, shop_key=shop.key, shop_name=shop.name, shop_url=shop.url,
            author=raw.get("author") or "匿名評論者", stars=parse_star_label(raw.get("stars_label", "")),
            time_text=raw.get("time_text", ""), text=raw.get("review_text", ""),
            profile=ProfileSummary(url=raw.get("profile_url", ""), avatar_url=raw.get("avatar_url", "")),
            photo_urls=list(dict.fromkeys(raw.get("photo_urls", []))), owner_reply=reply,
        )

    def _enrich_profile(self, review: ReviewSnapshot) -> None:
        if not review.profile.url or self.context is None or self._main_page is None:
            return
        if self._profile_visits:
            delay = random.uniform(
                self.settings.profile_delay_min_seconds,
                self.settings.profile_delay_max_seconds,
            )
            LOG.info("讀取下一個評論者頁面前等待 %.1f 秒", delay)
            time.sleep(delay)
        self._profile_visits += 1
        page = self._main_page
        try:
            page.goto(with_locale(review.profile.url, self.settings.locale), wait_until="domcontentloaded", timeout=30000)
            body = page.locator("body").inner_text(timeout=15000)
            review.profile.local_guide = bool(re.search(r"Local Guide|在地嚮導", body, re.I))
            review.profile.review_count = parse_count(body, r"則評論|評論|reviews?")
            review.profile.photo_count = parse_count(body, r"張相片|張照片|相片|照片|photos?")
            avatar = page.locator('img[src*="googleusercontent"], img[src*="ggpht"]')
            if avatar.count() and not review.profile.avatar_url:
                review.profile.avatar_url = avatar.first.get_attribute("src") or ""
        except Exception as exc:
            LOG.warning("讀取評論者摘要失敗（%s）：%s", review.author, exc)

    @staticmethod
    def _page_random_wait(page: Page, minimum: float, maximum: float) -> None:
        page.wait_for_timeout(int(random.uniform(minimum, maximum) * 1000))

    def _download_assets(self, review: ReviewSnapshot) -> None:
        target = self.settings.data_dir / "media" / review.shop_key / review.review_id
        target.mkdir(parents=True, exist_ok=True)
        for index, url in enumerate(review.photo_urls):
            path = self._download(url, target / f"photo-{index + 1:02d}")
            if path:
                review.photo_paths.append(self._stored_path(path))
        if review.profile.avatar_url:
            path = self._download(review.profile.avatar_url, target / "avatar")
            if path:
                review.profile.avatar_path = self._stored_path(path)

    def _stored_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return str(resolved.relative_to(self.settings.root.resolve()))
        except ValueError:
            return str(resolved)

    def _download(self, url: str, stem: Path) -> Path | None:
        try:
            response = self.http.get(url)
            response.raise_for_status()
            mime = response.headers.get("content-type", "").split(";")[0]
            suffix = {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
            path = stem.with_suffix(suffix)
            if not path.exists() or path.stat().st_size != len(response.content):
                path.write_bytes(response.content)
            return path
        except Exception as exc:
            LOG.warning("圖片下載失敗：%s", exc)
            return None

    def _reject_block_page(self, page: Page) -> None:
        title = page.title().lower()
        body = page.locator("body").inner_text(timeout=10000).lower()
        limited = [
            "you're seeing a limited view of google maps",
            "you are seeing a limited view of google maps",
            "google 地圖的部分內容可能受到限制",
            "google 地圖受限",
            "受限檢視",
        ]
        if any(word in body for word in limited):
            raise ScrapeError(
                "Google Maps 回傳受限模式，頁面未提供評論；"
                "登入雖然有效，但 Google 可能判定網路流量或瀏覽器環境異常"
            )
        blocked = ["unusual traffic", "not a robot", "驗證您不是機器人", "異常流量", "before you continue"]
        if any(word in title or word in body for word in blocked):
            raise ScrapeError("Google 顯示同意或驗證頁面；請使用 interactive-login 手動完成")

    def save_debug(self, page: Page, shop_key: str) -> None:
        folder = self.settings.debug_dir / f"{int(time.time())}-{shop_key}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            (folder / "page.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            LOG.warning("保存除錯 HTML 失敗：%s", exc)
        try:
            page.screenshot(
                path=str(folder / "page.png"), full_page=False,
                animations="disabled", timeout=15000,
            )
        except Exception as exc:
            LOG.warning("保存除錯截圖失敗（HTML 仍會保留）：%s", exc)
        diagnostics = getattr(self, "_pending_card_diagnostics", None)
        if diagnostics is not None:
            try:
                (folder / "review-cards.json").write_text(
                    json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:
                LOG.warning("保存評論卡片摘要失敗：%s", exc)
            finally:
                self._pending_card_diagnostics = None
        entries = sorted((p for p in self.settings.debug_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
        for old in entries[:-10]:
            import shutil
            shutil.rmtree(old, ignore_errors=True)

    def _save_card_diagnostics(self, cards: list[Locator]) -> None:
        diagnostics = []
        for index, card in enumerate(cards[:5]):
            try:
                diagnostics.append({
                    "index": index,
                    "outer_html": card.evaluate("node => node.outerHTML"),
                    "inner_text": card.inner_text(),
                    "extracted": self._extract_card(card),
                })
            except Exception as exc:
                diagnostics.append({"index": index, "error": str(exc)})
        self._pending_card_diagnostics = diagnostics
