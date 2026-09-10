"""Resolve Google shop links and append registrations without changing old keys."""
from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import tomllib
import unicodedata
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

import httpx

from .config import ShopConfig
from .lock import process_lock


GOOGLE_HOSTS = frozenset({
    "google.com", "www.google.com", "maps.google.com",
    "google.com.tw", "www.google.com.tw", "maps.google.com.tw",
})
SHORT_HOSTS = frozenset({"share.google", "maps.app.goo.gl", "goo.gl"})
MAX_URL_LENGTH = 16384


class RegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedShop:
    url: str
    name: str
    note: str = ""


def checked_google_url(value: str) -> str:
    value = value.strip()
    if len(value) > MAX_URL_LENGTH or re.search(r"[\x00-\x20\\]", value):
        raise RegistrationError("網址格式不正確，請只貼上完整的 Google 分享網址。")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in GOOGLE_HOSTS | SHORT_HOSTS
            and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
        )
    except ValueError:
        valid = False
    if not valid:
        raise RegistrationError("請使用 Google Maps、share.google 或 maps.app.goo.gl 的 HTTPS 網址。")
    return value


def normalize_shop_url(value: str) -> ResolvedShop | None:
    """Normalize a single-shop target; never rewrite existing stored URLs."""
    value = checked_google_url(value)
    parsed = urlsplit(value)
    if parsed.hostname not in GOOGLE_HOSTS:
        return None
    params = parse_qs(parsed.query)
    path = unquote(parsed.path)
    maps_path = path == "/maps" or path.startswith("/maps/")
    if not maps_path and path != "/search":
        return None
    if maps_path and any(segment in path.split("/") for segment in ("dir", "contrib", "reviews")):
        raise RegistrationError("這是路線或評論者頁面，請分享店家本身的 Google Maps 網址。")
    query = (params.get("query") or params.get("q") or [""])[0].strip()
    name = ""
    match = re.search(r"/maps/place/([^/]+)", path)
    if match:
        name = match.group(1).replace("+", " ")
    elif query and not re.match(r"^(?:place_id:|0x|\d+\s*,)", query):
        name = query
    cid = (params.get("cid") or params.get("ludocid") or [""])[0]
    feature = (params.get("ftid") or [""])[0]
    feature_match = re.search(r"0x[0-9a-f]{1,16}:0x([0-9a-f]{1,16})(?![0-9a-f])", feature or unquote(value), re.I)
    if not cid and feature_match:
        cid = str(int(feature_match.group(1), 16))
    if cid and len(cid) <= 20 and cid.isascii() and cid.isdigit() and 0 < int(cid) < 2**64:
        return ResolvedShop(f"https://www.google.com/maps?cid={int(cid)}", name[:120])
    place_id = (params.get("query_place_id") or [""])[0]
    if not place_id and query.startswith("place_id:"):
        place_id = query.removeprefix("place_id:")
    if place_id and re.fullmatch(r"[A-Za-z0-9_-]{5,256}", place_id):
        return ResolvedShop("https://www.google.com/maps/search/?" + urlencode({
            "api": "1", "query": name or place_id, "query_place_id": place_id,
        }), name[:120])
    if match:
        # Keep the place path/data but drop volatile tracking and locale queries.
        return ResolvedShop("https://www.google.com" + quote(path, safe="/!:@,+-._~"), name[:120])
    knowledge_id = (params.get("kgmid") or [""])[0]
    if query and (maps_path or re.fullmatch(r"/[gm]/[A-Za-z0-9_]+", knowledge_id)):
        return ResolvedShop(
            "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": query}),
            name[:120],
            "這個分享連結提供的是店家搜尋資訊，已轉成 Maps 搜尋網址；請開啟預覽確認是正確店家。",
        )
    return None


class _RedirectParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            if str(values.get("http-equiv", "")).lower() == "refresh":
                match = re.search(r"url\s*=\s*(.+)", values.get("content") or "", re.I)
                if match:
                    self.targets.append(match.group(1).strip(" '\""))
            elif values.get("property") == "og:url" and values.get("content"):
                self.targets.append(values["content"])
        elif tag == "link" and values.get("rel") == "canonical" and values.get("href"):
            self.targets.append(values["href"])


def resolve_shop_url(value: str, *, transport: httpx.BaseTransport | None = None) -> ResolvedShop:
    current = checked_google_url(value)
    seen: set[str] = set()
    deadline = time.monotonic() + 25
    try:
        with httpx.Client(
            transport=transport, follow_redirects=False, timeout=10,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"},
        ) as client:
            for _ in range(8):
                checked_google_url(current)  # Validate every hop before network I/O.
                if current in seen or time.monotonic() >= deadline:
                    raise RegistrationError("分享網址轉址過多或逾時，請改貼瀏覽器中的店家完整網址。")
                seen.add(current)
                resolved = normalize_shop_url(current)
                if resolved:
                    return resolved
                parsed = urlsplit(current)
                if parsed.hostname in GOOGLE_HOSTS and parsed.path not in {"/share.google", "/url"}:
                    raise RegistrationError("這個 Google 連結沒有指向可辨識的店家。請改貼 Maps 店家網址。")
                if parsed.path == "/url":
                    query = parse_qs(parsed.query)
                    target = (query.get("url") or query.get("q") or [""])[0]
                    current = checked_google_url(target)
                    continue
                with client.stream("GET", current, timeout=min(10, max(1, deadline-time.monotonic()))) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise RegistrationError("Google 分享網址沒有提供轉址目標。")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_000_000 or time.monotonic() >= deadline:
                            raise RegistrationError("Google 分享頁面過大或逾時，請改貼 Maps 店家完整網址。")
                    html = body.decode("utf-8", errors="replace")
                parser = _RedirectParser()
                parser.feed(html)
                for target in parser.targets:
                    candidate = checked_google_url(urljoin(current, unescape(target)))
                    if candidate not in seen:
                        current = candidate
                        break
                else:
                    raise RegistrationError("無法從分享頁取得店家網址，請在 Google Maps 開啟店家後複製網址。")
            raise RegistrationError("分享網址轉址過多，請改貼 Maps 店家完整網址。")
    except httpx.HTTPError as exc:
        raise RegistrationError("暫時無法連線到 Google 解析網址，請稍後重試或貼上店家完整網址。") from exc


def _identity(url: str) -> str:
    try:
        resolved = normalize_shop_url(url)
    except RegistrationError:
        resolved = None
    if resolved:
        params = parse_qs(urlsplit(resolved.url).query)
        if params.get("query_place_id"):
            return "place_id:" + params["query_place_id"][0]
        return resolved.url
    return url.strip()


def _name_key(name: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).casefold()


def add_shop(config_path: Path, *, name: str, url: str) -> ShopConfig:
    name = name.strip()
    if not name or len(name) > 120 or any(ord(ch) < 32 for ch in name):
        raise RegistrationError("請填寫 1 至 120 字的店家名稱，且不要包含換行。")
    target = normalize_shop_url(url)
    if target is None:
        raise RegistrationError("請先解析店家網址。")
    config_path = config_path.resolve()
    # Concurrent web submissions serialize; the monitor sees either complete
    # old bytes or complete new bytes through the atomic replacement below.
    with process_lock(config_path.with_name(config_path.name + ".lock")):
        original = config_path.read_bytes()
        raw = tomllib.loads(original.decode("utf-8"))
        shops = raw.get("shops", [])
        for shop in shops:
            if _identity(str(shop.get("url", ""))) == _identity(target.url):
                state = "已在監控清單中" if shop.get("enabled", True) else "已存在但尚未啟用，請在設定檔啟用"
                raise RegistrationError(f"這家店{state}：{shop.get('name', '')}")
            if _name_key(str(shop.get("name", ""))) == _name_key(name):
                raise RegistrationError("已有同名店家。若是不同分店，請在名稱加上分店或地址。")
        if sum(bool(shop.get("enabled", True)) for shop in shops) >= 10:
            raise RegistrationError("目前最多可啟用 10 家店，請先在設定檔停用不再監控的店家。")
        newline = "\r\n" if b"\r\n" in original else "\n"
        block = newline.join([
            "", "", "[[shops]]", f"name = {json.dumps(name, ensure_ascii=False)}",
            f"url = {json.dumps(target.url, ensure_ascii=False)}", "enabled = true", "",
        ])
        updated = original + block.encode("utf-8")
        tomllib.loads(updated.decode("utf-8"))
        backup = config_path.with_name(config_path.name + ".before-add.bak")
        temporary: str | None = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".shop-config-", dir=config_path.parent)
            with os.fdopen(fd, "wb") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(config_path.stat().st_mode))
            backup.write_bytes(original)
            os.chmod(backup, stat.S_IMODE(config_path.stat().st_mode))
            if config_path.read_bytes() != original:
                raise RegistrationError("設定檔剛被其他程序修改，請重新整理後再新增。")
            os.replace(temporary, config_path)
            temporary = None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
    return ShopConfig(name=name, url=target.url)
