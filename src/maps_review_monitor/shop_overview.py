"""Observed shop fields, immutable versions and atomic change notifications.

Missing fields mean unknown, never deleted. A snapshot merges only successfully
observed fields; its observed_fields records which values were checked this time.
"""
from __future__ import annotations

import json
import re
from hashlib import sha256
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from PIL import Image, ImageOps

FIELDS = {
    "name": "店名", "rating": "總評分", "review_count": "評論總數",
    "address": "地址", "phone": "電話", "hours": "營業時間",
    "website": "網站", "cover": "封面照片",
}


def clean(value: str) -> str:
    return " ".join(value.split())


def normalize_overview(raw: dict) -> dict:
    """Normalize only explicit observations; do not infer absence from DOM gaps."""
    result = {}
    for key in ("name", "address", "phone"):
        value = clean(str(raw.get(key) or ""))
        if value:
            result[key] = value
    website = str(raw.get("website") or "").strip()
    if urlsplit(website).scheme in {"https", "http"} and urlsplit(website).hostname:
        result["website"] = website
    rating = re.search(r"(?<!\d)([0-5](?:[.,]\d)?)\s*(?:顆星|星|stars?)", str(raw.get("rating") or ""), re.I)
    if rating:
        result["rating"] = float(rating[1].replace(",", "."))
    count = re.search(r"([\d,，]+)\s*(?:則評論|篇評論|reviews?)", str(raw.get("review_count") or ""), re.I)
    if count:
        result["review_count"] = int(count[1].replace(",", "").replace("，", ""))
    hours = [clean(str(row)) for row in raw.get("hours", []) if clean(str(row))]
    # A current "open now / closes at" summary is not the weekly schedule.
    if len(hours) == 7:
        result["hours"] = sorted(hours)
    return result


def display(value) -> str:
    if value is None:
        return "未確認"
    if isinstance(value, dict):
        return str(value.get("path", ""))
    if isinstance(value, list):
        return "\n".join(value)
    return str(value)


def _cover_pixels(cover: dict, project_root: Path, media_root: Path) -> str | None:
    """Hash decoded display pixels, not JPEG/PNG metadata or storage names."""
    try:
        path = Path(cover.get("path", ""))
        path = (path if path.is_absolute() else project_root / path).resolve()
        if not path.is_relative_to(media_root.resolve()) or not path.is_file():
            return None
        if path.stat().st_size > 10_000_000:
            return None
        with Image.open(path) as source:
            if source.width * source.height > 16_000_000 or getattr(source, "n_frames", 1) != 1:
                return None
            # Orientation affects appearance; other EXIF fields do not. Keep
            # color-profile identity so different rendering is not suppressed.
            profile = source.info.get("icc_profile", b"") or b""
            image = ImageOps.exif_transpose(source).convert("RGBA")
            digest = sha256()
            digest.update(f"rgba-v1:{image.width}:{image.height}:".encode())
            digest.update(sha256(profile).digest())
            digest.update(image.tobytes())
            return digest.hexdigest()
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return None


def covers_equal(before, after, project_root: Path, media_root: Path) -> bool:
    if before == after:
        return True
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    old_sha = before.get("sha256")
    if isinstance(old_sha, str) and re.fullmatch(r"[0-9a-f]{64}", old_sha) and old_sha == after.get("sha256"):
        return True
    old_pixels = _cover_pixels(before, project_root, media_root)
    return old_pixels is not None and old_pixels == _cover_pixels(after, project_root, media_root)


def history(db, shop_key: str, page: int = 1, size: int = 20) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM shop_overview_versions WHERE shop_key=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (shop_key, size, (page - 1) * size),
    )
    return [{"id": row["id"], "captured_at": row["captured_at"],
             "values": json.loads(row["snapshot_json"]),
             "changes": json.loads(row["changes_json"]),
             "observed_fields": json.loads(row["observed_fields_json"])} for row in rows]


def record(db, shop, observed: dict, *, notify: bool) -> bool:
    """Commit a new version and its outbox together. Initial snapshot is silent."""
    from .database import NOTIFICATION_TTL, utcnow
    from .notifications import split_text

    observed = {key: value for key, value in observed.items() if key in FIELDS and value is not None}
    if not observed.get("name"):
        return False
    now = utcnow()
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
    with db.transaction():
        prior = history(db, shop.key, size=1)
        old = prior[0]["values"] if prior else {}
        if "cover" in observed and "cover" in old and covers_equal(
            old["cover"], observed["cover"], db.project_root, db.media_root
        ):
            # Keep the original file reference even when some other field has
            # changed. Equivalent encodings must not leak into new snapshots.
            observed["cover"] = old["cover"]
        merged = {**old, **observed}
        changes = {key: {"before": old.get(key), "after": value}
                   for key, value in observed.items() if old.get(key) != value}
        if not changes:
            return False
        cursor = db.conn.execute(
            "INSERT INTO shop_overview_versions(shop_key,captured_at,snapshot_json,changes_json,observed_fields_json) VALUES(?,?,?,?,?)",
            (shop.key, now, encode(merged), encode(changes if prior else {}), encode(sorted(observed))),
        )
        if not prior or not notify:
            return False
        version_id = cursor.lastrowid
        local_time = datetime.fromisoformat(now).astimezone(ZoneInfo(db.timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        lines = ["🏪 店家總覽已變更", f"店家：{shop.name}", f"偵測時間：{local_time}", f"歷史版本：#{version_id}"]
        for key, change in changes.items():
            lines.extend(["", FIELDS[key], "原值：" + display(change["before"]), "新值：" + display(change["after"])])
        lines.extend(["", shop.url])
        payload = {"shop_key": shop.key, "version_id": version_id, "changes": changes}
        event = db.conn.execute(
            "INSERT INTO events(event_key,event_type,shop_key,payload_json,created_at) VALUES(?,?,?,?,?)",
            (f"shop_overview:{shop.key}:{version_id}", "shop_overview_changed", shop.key, encode(payload), now),
        )
        expires = (datetime.fromisoformat(now) + NOTIFICATION_TTL).isoformat()
        parts = [{"kind": "text", "text": text} for text in split_text("\n".join(lines))]
        if "cover" in changes:
            for side, label in (("before", "原封面"), ("after", "新封面")):
                cover = changes["cover"][side]
                if not isinstance(cover, dict) or not cover.get("path"):
                    continue
                path = Path(cover["path"])
                path = (path if path.is_absolute() else db.project_root / path).resolve()
                if path.is_relative_to(db.media_root) and path.is_file():
                    parts.extend([{"kind": "text", "text": f"{shop.name} · {label} · 版本 #{version_id}"},
                                  {"kind": "media", "paths": [str(path)]}])
        for number, part in enumerate(parts):
            db.conn.execute(
                "INSERT INTO notification_parts(event_id,part_no,kind,payload_json,next_attempt_at,expires_at) VALUES(?,?,?,?,?,?)",
                (event.lastrowid, number, part["kind"], encode(part), now, expires),
            )
    return True
