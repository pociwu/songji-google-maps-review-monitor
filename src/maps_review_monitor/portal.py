"""Tailnet-only public portal for monitored shops and managed content."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Any
import unicodedata
from urllib.parse import quote
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .config import load_settings
from .database import Database
from .evidence_inputs import canonical_contributor_id
from .review_time_analysis import build_review_time_analysis, resolve_review_posted_time
from . import __version__
from .shop_registration import RegistrationError, add_shop, resolve_shop_url, checked_google_url
from .shop_overview import FIELDS as OVERVIEW_FIELDS, display as overview_display, history as overview_history


PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
TIME_WINDOWS = {"30d": 30, "90d": 90, "180d": 180, "365d": 365}
PUBLIC_MEDIA_SUFFIXES = frozenset(
    {
        ".avif",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4v",
        ".mov",
        ".mp4",
        ".ogv",
        ".png",
        ".webm",
        ".webp",
    }
)


def load_content_assets(path: Path) -> list[dict[str, Any]]:
    """Load and minimally validate operator-maintained content.yaml."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    entries = raw.get("content", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("content.yaml 的 content 必須是清單")

    result: list[dict[str, Any]] = []
    required = {"id", "shop_key", "published_at", "title", "body"}
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"content.yaml 第 {index} 筆必須是物件")
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"content.yaml 第 {index} 筆缺少必要欄位：{', '.join(sorted(missing))}")
        value = dict(entry)
        value["photos"] = _string_list(value.get("photos", []), f"第 {index} 筆 photos")
        value["videos"] = _string_list(value.get("videos", []), f"第 {index} 筆 videos")
        result.append(value)
    return sorted(result, key=lambda item: str(item["published_at"]), reverse=True)


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"content.yaml {field} 必須是字串清單")
    return value


class ResolveShopInput(BaseModel):
    url: str = Field(min_length=1, max_length=16384)


class AddShopInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    token: str = Field(min_length=1, max_length=40000)


def create_app(config_path: str = "config.toml") -> FastAPI:
    config_file = Path(config_path).resolve()
    settings = load_settings(config_path)
    app = FastAPI(title="Songji 店家展示頁", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    signing_key = secrets.token_bytes(32)
    resolver_slots = threading.BoundedSemaphore(2)

    def require_same_origin(request: Request) -> None:
        # The tailnet is the access boundary. Reject cross-site forms/fetches
        # without adding a login to this operator-facing portal.
        origin = request.headers.get("origin", "")
        if (
            origin != str(request.base_url).rstrip("/")
            or request.headers.get("x-requested-with") != "SongjiPortal"
        ):
            raise HTTPException(403, "請從本網站的新增店家表單送出。")

    @app.post("/api/shops/resolve")
    def resolve_shop(body: ResolveShopInput, request: Request) -> JSONResponse:
        require_same_origin(request)
        if not resolver_slots.acquire(blocking=False):
            raise HTTPException(429, "目前正在解析其他網址，請稍後重試。")
        try:
            resolved = resolve_shop_url(body.url)
        except RegistrationError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            resolver_slots.release()
        payload = json.dumps({"url": resolved.url, "expires": int(time.time()) + 900}, ensure_ascii=True)
        signature = hmac.new(signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return JSONResponse({
            "url": resolved.url, "name": resolved.name, "note": resolved.note,
            "token": signature + "." + payload,
        }, headers={"Cache-Control": "no-store"})

    @app.post("/api/shops", status_code=201)
    def register_shop(body: AddShopInput, request: Request) -> JSONResponse:
        require_same_origin(request)
        try:
            signature, payload = body.token.split(".", 1)
            expected = hmac.new(signing_key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            resolved = json.loads(payload)
            if resolved["expires"] < time.time():
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "網址預覽已失效，請重新解析後再新增。")
        try:
            shop = add_shop(config_file, name=body.name, url=resolved["url"])
        except RegistrationError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, "另一筆店家設定正在儲存，請稍後重試。") from exc
        except OSError as exc:
            raise HTTPException(503, "無法儲存設定檔，請確認網頁服務使用者可寫入專案目錄及 config.toml。") from exc
        return JSONResponse({
            "shop_key": shop.key, "name": shop.name, "url": shop.url,
            "message": "店家已加入監控，下一輪排程會建立首次基準；既有評論不會補發通知。",
        }, status_code=201)

    def portal_assets() -> list[dict[str, Any]]:
        return _decorate_assets(load_content_assets(settings.data_dir / "portal" / "content.yaml"))

    def shop_statuses() -> list[dict[str, Any]]:
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            statuses = list(db.iter_shop_statuses())
            analysis_counts = db.analysis_shop_counts()
        finally:
            db.close()
        # Reload registrations immediately; the next monitor invocation also
        # loads the complete config, without restarting either service.
        current_shops = load_settings(config_file).shops
        configured = {shop.key: shop.enabled for shop in current_shops}
        known_keys = {item["shop_key"] for item in statuses}
        for shop in current_shops:
            if shop.key not in known_keys:
                statuses.append({
                    "shop_key": shop.key, "name": shop.name, "url": shop.url,
                    "initialized": False, "consecutive_failures": 0,
                    "last_checked_at": None, "last_success_at": None,
                    "latest_review_at": None, "last_result": "pending",
                })
        for item in statuses:
            try:
                item["google_url"] = checked_google_url(item["url"])
            except RegistrationError:
                item["google_url"] = ""
            item["enabled"] = configured.get(item["shop_key"], False)
            item.update(analysis_counts.get(item["shop_key"], {"similar": 0, "suspected": 0}))
        return statuses

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        assets_by_shop = defaultdict(list)
        for asset in portal_assets():
            assets_by_shop[asset["shop_key"]].append(asset)
        shops = shop_statuses()
        for shop in shops:
            shop["latest_asset"] = (assets_by_shop.get(shop["shop_key"]) or [None])[0]
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"shops": shops, "version": __version__}
        )

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "") -> HTMLResponse:
        query = q.strip()[:100]
        shops = shop_statuses()
        assets = portal_assets()
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            reviews = [_decorate_review(item, settings.timezone) for item in db.iter_reviews()]
        finally:
            db.close()
        results = build_search_results(query, shops, reviews, assets)
        return TEMPLATES.TemplateResponse(
            request,
            "search.html",
            {"query": query, **results},
        )

    @app.get("/shops/{shop_key}", response_class=HTMLResponse)
    def shop_page(
        request: Request, shop_key: str, page: int = 1, view: str = "all", overview_page: int = 1
    ) -> HTMLResponse:
        if page < 1 or overview_page < 1:
            raise HTTPException(404)
        if view not in {"all", "similar", "suspected"}:
            raise HTTPException(404)
        status = next((item for item in shop_statuses() if item["shop_key"] == shop_key), None)
        if status is None:
            raise HTTPException(404, "找不到店家")
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            overview_latest = overview_history(db, shop_key, size=1)
            overview_versions = overview_history(db, shop_key, page=overview_page)
            overview_total = db.conn.execute("SELECT count(*) FROM shop_overview_versions WHERE shop_key=?", (shop_key,)).fetchone()[0]
            for version in overview_latest + overview_versions:
                version["captured_at"] = datetime.fromisoformat(version["captured_at"]).astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
                cover = version["values"].get("cover")
                version["cover_url"] = _media_url(cover["path"]) if cover else ""
            reviews = [_decorate_review(item, settings.timezone) for item in db.iter_reviews(shop_key)]
            review_analysis = db.review_analysis(shop_key)
            groups = db.analysis_groups(shop_key=shop_key)
            group_links: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            visible_groups = {group["group_id"]: group for group in groups}
            run_id = db.current_analysis_run_id()
            if run_id is not None:
                for row in db.conn.execute(
                    """SELECT group_id,shop_key,review_id,max_lexical,max_semantic
                       FROM analysis_group_reviews
                       WHERE run_id=? AND shop_key=?""",
                    (run_id, shop_key),
                ):
                    group = visible_groups.get(row["group_id"])
                    if group is None:
                        continue
                    group_links[(row["shop_key"], row["review_id"])].append(
                        {
                            "group_id": row["group_id"],
                            "label": group["label"],
                            "level": group["evidence"]["level"],
                            "level_label": group["evidence"]["level_label"],
                            "max_lexical": row["max_lexical"],
                            "max_semantic": row["max_semantic"],
                        }
                    )
        finally:
            db.close()
        for review in reviews:
            key = (review["shop_key"], review["review_id"])
            value = review_analysis.get(key, {})
            links = group_links.get(key, [])
            level_priority = {"content_similar": 0, "review": 1, "suspicious_high": 2}
            links.sort(
                key=lambda item: (
                    level_priority.get(item["level"], -1),
                    max(item["max_lexical"], item["max_semantic"]),
                ),
                reverse=True,
            )
            strongest = links[0] if links else {}
            review["analysis_label"] = strongest.get("label", value.get("label", ""))
            review["analysis_level"] = strongest.get("level", "")
            review["analysis_level_label"] = strongest.get("level_label", "")
            review["analysis_direction"] = value.get("direction", "")
            review["max_lexical"] = strongest.get(
                "max_lexical", value.get("max_lexical", 0)
            )
            review["max_semantic"] = strongest.get(
                "max_semantic", value.get("max_semantic", 0)
            )
            review["analysis_groups"] = links
        if view == "similar":
            reviews = [item for item in reviews if item["analysis_label"] == "highly_similar"]
        elif view == "suspected":
            reviews = [item for item in reviews if item["analysis_label"] == "suspected"]
        page_size = 20
        start = (page - 1) * page_size
        shown = reviews[start : start + page_size]
        return TEMPLATES.TemplateResponse(
            request,
            "shop.html",
            {
                "shop": status,
                "overview": overview_latest[0] if overview_latest else None,
                "overview_versions": overview_versions,
                "overview_page": overview_page,
                "overview_has_next": overview_page * 20 < overview_total,
                "overview_fields": OVERVIEW_FIELDS,
                "overview_display": overview_display,
                "assets": [asset for asset in portal_assets() if asset["shop_key"] == shop_key],
                "reviews": shown,
                "groups": groups,
                "view": view,
                "review_count": len(reviews),
                "page": page,
                "has_next": len(reviews) > start + page_size,
            },
        )

    @app.get("/analysis", response_class=HTMLResponse)
    def analysis_page(
        request: Request,
        scope: str = "same",
        direction: str = "all",
        sort: str = "count",
    ) -> HTMLResponse:
        if scope not in {"same", "cross"} or direction not in {"all", "positive", "negative", "mixed"}:
            raise HTTPException(404)
        if sort not in {"count", "similarity", "latest"}:
            raise HTTPException(404)
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            groups = db.analysis_groups(scope=scope)
            status = db.analysis_status()
        finally:
            db.close()
        if direction != "all":
            groups = [group for group in groups if group["direction"] == direction]
        sort_keys = {
            "count": lambda group: (group["review_count"], group["max_semantic"]),
            "similarity": lambda group: (max(group["max_lexical"], group["max_semantic"]), group["review_count"]),
            "latest": lambda group: (group["latest_at"] or "", group["review_count"]),
        }
        groups.sort(key=sort_keys[sort], reverse=True)
        return TEMPLATES.TemplateResponse(
            request,
            "analysis.html",
            {"groups": groups, "scope": scope, "direction": direction, "sort": sort, "status": status},
        )

    @app.get("/analysis/groups/{group_id}", response_class=HTMLResponse)
    def analysis_group_page(request: Request, group_id: str) -> HTMLResponse:
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            result = db.analysis_group(group_id)
        finally:
            db.close()
        if result is None:
            raise HTTPException(404)
        group, reviews = result
        _decorate_evidence_media(group["evidence"])
        data_notes = []
        if group["evidence"]["is_legacy"]:
            data_notes.append("此群組來自舊版規則，已降為待人工複核；下次完整分析後會改用證據模型 v2。")
        elif group["evidence"].get("unsupported_version"):
            data_notes.append("此群組使用目前程式無法驗證的新版證據格式，已安全降級；請更新程式或重新執行完整分析。")
        if any(
            not canonical_contributor_id(
                str((item.get("profile") or {}).get("url", ""))
            )
            for item in reviews
        ):
            data_notes.append("資料不足：部分評論缺少可解析的 Google contributor ID，不能作為評論者網絡佐證。")
        if any(not (item.get("estimated_posted_date") or item.get("back_calculated_at")) for item in reviews):
            data_notes.append("資料不足：部分評論缺少可驗證發布日期，不能作為時間集中佐證。")
        data_notes.append("結論只涵蓋目前監控店家與已擷取評論；證據等級是複核排序，不是造假機率。")
        return TEMPLATES.TemplateResponse(
            request,
            "analysis-group.html",
            {
                "group": group,
                "reviews": [_decorate_review(item, settings.timezone) for item in reviews],
                "data_notes": data_notes,
            },
        )

    @app.get("/reviewers", response_class=HTMLResponse)
    def same_name_reviewers(request: Request, q: str = "") -> HTMLResponse:
        query = q.strip()[:80]
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            reviews = [_decorate_review(item, settings.timezone) for item in db.iter_reviews()]
        finally:
            db.close()
        groups = (
            search_similar_reviewer_names(query, reviews)
            if query
            else build_same_name_groups(reviews)
        )
        return TEMPLATES.TemplateResponse(
            request,
            "reviewers.html",
            {"groups": groups, "query": query},
        )

    @app.get("/review-times", response_class=HTMLResponse)
    def review_times_page(
        request: Request, shop: str = "all", window: str = "90d"
    ) -> HTMLResponse:
        if window not in TIME_WINDOWS:
            raise HTTPException(404)
        shops = shop_statuses()
        valid_shop_keys = {item["shop_key"] for item in shops}
        if shop != "all" and shop not in valid_shop_keys:
            raise HTTPException(404, "找不到店家")
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            all_reviews = list(db.iter_reviews())
        finally:
            db.close()
        selected_reviews = (
            all_reviews
            if shop == "all"
            else [item for item in all_reviews if item.get("shop_key") == shop]
        )
        days = TIME_WINDOWS[window]
        analysis = build_review_time_analysis(selected_reviews, settings.timezone, days)
        shop_summaries = []
        if shop == "all":
            reviews_by_shop: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for review in all_reviews:
                reviews_by_shop[str(review.get("shop_key") or "")].append(review)
            for item in shops:
                result = build_review_time_analysis(
                    reviews_by_shop.get(item["shop_key"], []), settings.timezone, days
                )
                shop_summaries.append(
                    {
                        "shop_key": item["shop_key"],
                        "name": item["name"],
                        "period_count": result["period_count"],
                        "pattern_eligible_count": result["pattern_eligible_count"],
                        "verdict": result["verdict"],
                        "finding_count": len(result["findings"]),
                    }
                )
        status_rank = {"possible": 0, "none": 1, "insufficient": 2}
        shop_summaries.sort(
            key=lambda item: (
                status_rank[item["verdict"]["status"]],
                -item["finding_count"],
                -item["period_count"],
                item["name"],
            )
        )
        selected_shop_name = "全部店家"
        if shop != "all":
            selected_shop_name = next(
                item["name"] for item in shops if item["shop_key"] == shop
            )
        return TEMPLATES.TemplateResponse(
            request,
            "review-times.html",
            {
                "analysis": analysis,
                "shops": shops,
                "shop_summaries": shop_summaries,
                "selected_shop": shop,
                "selected_shop_name": selected_shop_name,
                "selected_window": window,
                "window_options": [
                    {"value": "30d", "label": "近 30 天"},
                    {"value": "90d", "label": "近 90 天"},
                    {"value": "180d", "label": "近 180 天"},
                    {"value": "365d", "label": "近 365 天"},
                ],
            },
        )

    @app.get("/api/analysis-status")
    def analysis_status() -> JSONResponse:
        db = Database(settings.database_path, settings.timezone, settings.root)
        try:
            status = db.analysis_status() or {
                "status": "none", "stage": "尚未執行", "percent": 0, "processed": 0, "total": 0
            }
        finally:
            db.close()
        return JSONResponse(status)

    @app.get("/media/{stored_path:path}")
    def media(stored_path: str) -> FileResponse:
        target = _public_media_path(
            stored_path,
            project_root=settings.root,
            media_roots=(settings.data_dir / "media", settings.data_dir / "portal"),
        )
        if target is None:
            raise HTTPException(404)
        return FileResponse(target, headers={"X-Content-Type-Options": "nosniff"})

    return app


def _decorate_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for asset in assets:
        asset["photo_urls"] = [_media_url(path) for path in asset["photos"]]
        asset["video_items"] = [
            {"url": video if video.startswith(("https://", "http://")) else _media_url(video),
             "local": not video.startswith(("https://", "http://"))}
            for video in asset["videos"]
        ]
    return assets


def _decorate_review(
    review: dict[str, Any], timezone_name: str = "Asia/Taipei"
) -> dict[str, Any]:
    profile = review.get("profile") or {}
    review["photo_urls_local"] = [_media_url(path) for path in review.get("photo_paths", [])]
    review["avatar_url_local"] = _media_url(profile["avatar_path"]) if profile.get("avatar_path") else ""
    posted = resolve_review_posted_time(review, timezone_name)
    review["posted_at_display"] = posted["display"] if posted else ""
    review["posted_at_precision"] = posted["precision"] if posted else "unknown"
    review["posted_at_source"] = posted["source_label"] if posted else ""
    return review


def _decorate_evidence_media(evidence: dict[str, Any]) -> None:
    """Attach public URLs only to the exact files named by photo evidence."""
    for signal in evidence.get("signals") or []:
        if not isinstance(signal, dict) or signal.get("key") != "shared_photo":
            continue
        observed = signal.get("observed")
        if not isinstance(observed, dict):
            continue
        for match in observed.get("shared_matches") or []:
            if not isinstance(match, dict):
                continue
            for member in match.get("members") or []:
                if not isinstance(member, dict):
                    continue
                member["photo_urls"] = [
                    _media_url(str(path))
                    for path in member.get("photo_paths") or []
                    if str(path).strip()
                ]


def build_same_name_groups(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group equal display names across shops without assuming identity."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        author = str(review.get("author", "")).strip()
        key = _reviewer_name_key(author)
        if not key or key in {"匿名評論者", "anonymous"}:
            continue
        grouped[key].append(review)

    result = []
    for members in grouped.values():
        group = _build_reviewer_group(members)
        if group["shop_count"] < 2:
            continue
        result.append(group)
    return sorted(
        result,
        key=lambda item: (item["shop_count"], item["review_count"], item["author"]),
        reverse=True,
    )


def search_similar_reviewer_names(
    query: str, reviews: list[dict[str, Any]], limit: int = 50
) -> list[dict[str, Any]]:
    """Find reviewer display names that are exact, partial, or visibly similar."""
    query_key = _reviewer_name_key(query)
    if not query_key:
        return []

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        author = str(review.get("author", "")).strip()
        key = _reviewer_name_key(author)
        if not key or key in {"匿名評論者", "anonymous"}:
            continue
        grouped[key].append(review)

    result: list[dict[str, Any]] = []
    for candidate_key, members in grouped.items():
        similarity = SequenceMatcher(
            None, query_key, candidate_key, autojunk=False
        ).ratio()
        partial = (
            min(len(query_key), len(candidate_key)) >= 2
            and (query_key in candidate_key or candidate_key in query_key)
        )
        shortest = min(len(query_key), len(candidate_key))
        if shortest <= 1:
            threshold = 1.0
        elif len(query_key) == 2:
            threshold = 0.80
        elif len(query_key) <= 4:
            threshold = 0.66
        else:
            threshold = 0.75
        if not partial and similarity < threshold:
            continue

        group = _build_reviewer_group(members)
        if candidate_key == query_key:
            match_type = "exact"
            match_text = "完全符合"
        elif partial:
            match_type = "partial"
            match_text = "部分符合"
        else:
            match_type = "similar"
            match_text = "相似名稱"
        group.update(
            {
                "similarity": similarity,
                "similarity_percent": round(similarity * 100),
                "match_type": match_type,
                "match_text": match_text,
            }
        )
        result.append(group)

    match_rank = {"exact": 2, "partial": 1, "similar": 0}
    result.sort(
        key=lambda item: (
            -match_rank[item["match_type"]],
            -item["similarity"],
            -item["shop_count"],
            -item["review_count"],
            item["author"].casefold(),
        )
    )
    return result[:limit]


def _reviewer_name_key(value: str) -> str:
    """Normalize a display name while retaining Unicode letters and digits."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _build_reviewer_group(members: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        members, key=lambda item: str(item.get("first_seen_at", "")), reverse=True
    )
    shops = {str(item.get("shop_key", "")) for item in members}
    contributor_ids = [
        canonical_contributor_id(
            str((item.get("profile") or {}).get("url", "")).strip()
        )
        for item in members
    ]
    known_profiles = {value for value in contributor_ids if value}
    missing_profiles = sum(not value for value in contributor_ids)
    variants = sorted(
        {str(item.get("author", "")).strip() for item in members if item.get("author")},
        key=str.casefold,
    )
    if len(shops) < 2:
        identity_status = "single_shop"
        identity_text = "目前只出現在 1 家店"
    elif len(known_profiles) == 1 and not missing_profiles:
        identity_status = "same_profile"
        identity_text = "相同 Google 個人檔案"
    elif len(known_profiles) > 1:
        identity_status = "different_profiles"
        identity_text = "同名但個人檔案不同，未確認為同一人"
    else:
        identity_status = "insufficient"
        identity_text = "資料不足，未確認為同一人"
    return {
        "author": ordered[0]["author"],
        "name_variants": variants,
        "shop_count": len(shops),
        "review_count": len(members),
        "profile_count": len(known_profiles),
        "missing_profiles": missing_profiles,
        "identity_status": identity_status,
        "identity_text": identity_text,
        "reviews": ordered,
    }


def build_search_results(
    query: str,
    shops: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Search public portal content using normalized, case-insensitive tokens."""
    tokens = _search_text(query).split()
    if not tokens:
        return {
            "shop_results": [],
            "review_results": [],
            "asset_results": [],
            "total": 0,
        }

    def matches(*values: Any) -> bool:
        haystack = _search_text(" ".join(str(value or "") for value in values))
        return all(token in haystack for token in tokens)

    shop_results = [
        shop for shop in shops if matches(shop.get("name"), shop.get("url"))
    ]
    review_results = []
    for review in reviews:
        reply = review.get("owner_reply") or {}
        if matches(
            review.get("shop_name"),
            review.get("author"),
            review.get("text"),
            reply.get("text"),
        ):
            review_results.append(review)
    shop_names = {shop["shop_key"]: shop["name"] for shop in shops}
    asset_results = []
    for asset in assets:
        if matches(
            shop_names.get(asset.get("shop_key"), ""),
            asset.get("title"),
            asset.get("body"),
        ):
            item = dict(asset)
            item["shop_name"] = shop_names.get(item.get("shop_key"), "未知店家")
            asset_results.append(item)
    return {
        "shop_results": shop_results,
        "review_results": review_results[:100],
        "asset_results": asset_results[:100],
        "total": len(shop_results) + len(review_results) + len(asset_results),
    }


def _search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _media_url(stored_path: str) -> str:
    return "/media/" + quote(stored_path.replace("\\", "/"), safe="/")


def _public_media_path(
    stored_path: str,
    *,
    project_root: Path,
    media_roots: tuple[Path, ...],
) -> Path | None:
    """Resolve a stored path only when it is a public image or video asset."""
    try:
        supplied = Path(stored_path)
        target = (
            supplied.resolve()
            if supplied.is_absolute()
            else (project_root / supplied).resolve()
        )
        if not target.is_file() or target.suffix.casefold() not in PUBLIC_MEDIA_SUFFIXES:
            return None

        for configured_root in media_roots:
            public_root = configured_root.resolve()
            try:
                relative = target.relative_to(public_root)
            except ValueError:
                continue
            if relative.parts and not any(part.startswith(".") for part in relative.parts):
                return target
    except (OSError, RuntimeError, ValueError):
        return None
    return None
