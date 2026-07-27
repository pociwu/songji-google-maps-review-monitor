from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from .config import Settings
from .database import Database


def export_reviews(settings: Settings, output: Path, fmt: str, shop_key: str | None = None) -> int:
    db = Database(settings.database_path, settings.timezone)
    try:
        rows = list(db.iter_reviews(shop_key))
    finally:
        db.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif fmt == "csv":
        fields = [
            "shop_name", "shop_url", "review_id", "author", "stars", "time_text", "text",
            "profile_url", "avatar_path", "local_guide", "profile_review_count", "profile_photo_count",
            "photo_urls", "photo_paths", "owner_reply_text", "owner_reply_time", "first_seen_at", "updated_at",
            "back_calculated_at", "estimated_posted_date", "time_parse_status",
        ]
        with output.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for item in rows:
                profile = item.get("profile", {})
                reply = item.get("owner_reply") or {}
                writer.writerow({
                    "shop_name": item.get("shop_name"), "shop_url": item.get("shop_url"),
                    "review_id": item.get("review_id"), "author": item.get("author"), "stars": item.get("stars"),
                    "time_text": item.get("time_text"), "text": item.get("text"),
                    "profile_url": profile.get("url"), "avatar_path": profile.get("avatar_path"),
                    "local_guide": profile.get("local_guide"), "profile_review_count": profile.get("review_count"),
                    "profile_photo_count": profile.get("photo_count"),
                    "photo_urls": json.dumps(item.get("photo_urls", []), ensure_ascii=False),
                    "photo_paths": json.dumps(item.get("photo_paths", []), ensure_ascii=False),
                    "owner_reply_text": reply.get("text"), "owner_reply_time": reply.get("time_text"),
                    "first_seen_at": item.get("first_seen_at"), "updated_at": item.get("updated_at"),
                    "back_calculated_at": item.get("back_calculated_at"),
                    "estimated_posted_date": item.get("estimated_posted_date"),
                    "time_parse_status": item.get("time_parse_status"),
                })
    else:
        raise ValueError("匯出格式只能是 csv 或 json")
    return len(rows)


def create_backup(settings: Settings, output: Path | None = None) -> Path:
    backup_dir = settings.root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    output = output or backup_dir / f"maps-review-monitor-{datetime.now():%Y%m%d-%H%M%S}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        temp_db = Path(tmp) / "reviews.sqlite3"
        if settings.database_path.exists():
            source = sqlite3.connect(settings.database_path)
            target = sqlite3.connect(temp_db)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if temp_db.exists():
                archive.write(temp_db, "data/reviews.sqlite3")
            if settings.data_dir.exists():
                for path in settings.data_dir.rglob("*"):
                    if path.is_file() and not path.name.startswith("reviews.sqlite3"):
                        archive.write(path, Path("data") / path.relative_to(settings.data_dir))
            for name in ("config.toml", ".env"):
                path = settings.root / name
                if path.exists():
                    archive.write(path, name)
    return output


def restore_backup(settings: Settings, archive_path: Path, force: bool = False) -> None:
    if settings.database_path.exists() and not force:
        raise RuntimeError("資料庫已存在；確認要覆蓋時請加 --force")
    root = settings.root.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (root / member.filename).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError("備份檔包含不安全路徑")
        archive.extractall(root)
