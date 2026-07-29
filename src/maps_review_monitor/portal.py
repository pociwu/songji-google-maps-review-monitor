"""Tailnet-only public portal for monitored shops and managed content."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
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
        finally:
            db.close()
        configured = {shop.key: shop.enabled for shop in settings.shops}
        for item in statuses:
            item["enabled"] = configured.get(item["shop_key"], False)
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
    def shop_page(request: Request, shop_key: str, page: int = 1) -> HTMLResponse:
        if page < 1:
            raise HTTPException(404)
        status = next((item for item in shop_statuses() if item["shop_key"] == shop_key), None)
        if status is None:
            raise HTTPException(404, "找不到店家")
        db = Database(settings.database_path, settings.timezone)
        try:
            reviews = [_decorate_review(item) for item in db.iter_reviews(shop_key)]
        finally:
            db.close()
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
                "page": page,
                "has_next": len(reviews) > start + page_size,
            },
        )

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
