"""Tailnet-only public portal for monitored shops and managed content."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import load_settings
from .database import Database


PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


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


def create_app(config_path: str = "config.toml") -> FastAPI:
    settings = load_settings(config_path)
    app = FastAPI(title="Songji 店家展示頁", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    def portal_assets() -> list[dict[str, Any]]:
        return _decorate_assets(load_content_assets(settings.data_dir / "portal" / "content.yaml"))

    def shop_statuses() -> list[dict[str, Any]]:
        db = Database(settings.database_path, settings.timezone)
        try:
            statuses = list(db.iter_shop_statuses())
            analysis_counts = db.analysis_shop_counts()
        finally:
            db.close()
        configured = {shop.key: shop.enabled for shop in settings.shops}
        for item in statuses:
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
        return TEMPLATES.TemplateResponse(request, "index.html", {"shops": shops})

    @app.get("/shops/{shop_key}", response_class=HTMLResponse)
    def shop_page(
        request: Request, shop_key: str, page: int = 1, view: str = "all"
    ) -> HTMLResponse:
        if page < 1:
            raise HTTPException(404)
        if view not in {"all", "similar", "suspected"}:
            raise HTTPException(404)
        status = next((item for item in shop_statuses() if item["shop_key"] == shop_key), None)
        if status is None:
            raise HTTPException(404, "找不到店家")
        db = Database(settings.database_path, settings.timezone)
        try:
            reviews = [_decorate_review(item) for item in db.iter_reviews(shop_key)]
            review_analysis = db.review_analysis(shop_key)
            groups = db.analysis_groups(shop_key=shop_key)
            group_links: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
            run_id = db.current_analysis_run_id()
            if run_id is not None:
                for row in db.conn.execute(
                    """SELECT group_id,shop_key,review_id FROM analysis_group_reviews
                       WHERE run_id=? AND shop_key=?""",
                    (run_id, shop_key),
                ):
                    group_links[(row["shop_key"], row["review_id"])].append(
                        {"group_id": row["group_id"]}
                    )
        finally:
            db.close()
        for review in reviews:
            key = (review["shop_key"], review["review_id"])
            value = review_analysis.get(key, {})
            review["analysis_label"] = value.get("label", "")
            review["analysis_direction"] = value.get("direction", "")
            review["max_lexical"] = value.get("max_lexical", 0)
            review["max_semantic"] = value.get("max_semantic", 0)
            review["analysis_groups"] = group_links.get(key, [])
        if view == "similar":
            reviews = [item for item in reviews if item["analysis_label"] in {"highly_similar", "suspected"}]
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
        db = Database(settings.database_path, settings.timezone)
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
        db = Database(settings.database_path, settings.timezone)
        try:
            result = db.analysis_group(group_id)
        finally:
            db.close()
        if result is None:
            raise HTTPException(404)
        group, reviews = result
        data_notes = []
        if any(not (item.get("profile") or {}).get("url") for item in reviews):
            data_notes.append("資料不足：部分評論缺少 Google 個人檔案網址，不能作為重複評論者佐證。")
        if any(not (item.get("estimated_posted_date") or item.get("back_calculated_at")) for item in reviews):
            data_notes.append("資料不足：部分評論缺少可驗證發布日期，不能作為時間集中佐證。")
        return TEMPLATES.TemplateResponse(
            request,
            "analysis-group.html",
            {
                "group": group,
                "reviews": [_decorate_review(item) for item in reviews],
                "data_notes": data_notes,
            },
        )

    @app.get("/api/analysis-status")
    def analysis_status() -> JSONResponse:
        db = Database(settings.database_path, settings.timezone)
        try:
            status = db.analysis_status() or {
                "status": "none", "stage": "尚未執行", "percent": 0, "processed": 0, "total": 0
            }
        finally:
            db.close()
        return JSONResponse(status)

    @app.get("/media/{stored_path:path}")
    def media(stored_path: str) -> FileResponse:
        target = (settings.root / stored_path).resolve()
        root = settings.root.resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

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


def _decorate_review(review: dict[str, Any]) -> dict[str, Any]:
    profile = review.get("profile") or {}
    review["photo_urls_local"] = [_media_url(path) for path in review.get("photo_paths", [])]
    review["avatar_url_local"] = _media_url(profile["avatar_path"]) if profile.get("avatar_path") else ""
    return review


def _media_url(stored_path: str) -> str:
    return "/media/" + quote(stored_path.replace("\\", "/"), safe="/")
