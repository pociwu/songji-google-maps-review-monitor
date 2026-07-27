from __future__ import annotations

from datetime import datetime
import io
import json
import logging
from pathlib import Path
import random
import time
from zoneinfo import ZoneInfo

import httpx
from PIL import Image

from .database import Database
from .models import ReviewSnapshot

LOG = logging.getLogger(__name__)
TEXT_LIMIT = 3900
PHOTO_SAFE_BYTES = 9_500_000


EVENT_TITLES = {
    "review_new": "🆕 新評論",
    "review_updated": "✏️ 評論已更新",
    "owner_reply_added": "💬 店家回覆新增／已更新",
    "owner_reply_removed": "🗑️ 店家回覆已移除",
}


def render_review(
    event_type: str, review: ReviewSnapshot, timezone_name: str, first_seen_at: str | None = None
) -> str:
    stars = "未知" if review.stars is None else f"{review.stars:g} / 5"
    detected = datetime.fromisoformat(first_seen_at) if first_seen_at else datetime.now(ZoneInfo(timezone_name))
    detected = detected.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
    back_calculated = ""
    if review.back_calculated_at:
        back_calculated = datetime.fromisoformat(review.back_calculated_at).astimezone(
            ZoneInfo(timezone_name)
        ).strftime("%Y-%m-%d %H:%M:%S %Z")
    profile_bits = []
    if review.profile.local_guide:
        profile_bits.append("Local Guide")
    if review.profile.review_count is not None:
        profile_bits.append(f"{review.profile.review_count} 則評論")
    if review.profile.photo_count is not None:
        profile_bits.append(f"{review.profile.photo_count} 張照片")
    lines = [
        EVENT_TITLES[event_type], f"店家：{review.shop_name}", f"評論者：{review.author}",
        f"星等：{stars}", f"Google 顯示時間：{review.time_text or '未提供'}", f"首次偵測：{detected}",
    ]
    if back_calculated:
        lines.append(f"回推發文時間：{back_calculated}")
    lines.extend(["", "評論內容：", review.text or "（無文字評論）"])
    if review.owner_reply:
        lines.extend(["", f"店家回覆（{review.owner_reply.time_text or '時間未提供'}）：", review.owner_reply.text])
    if profile_bits:
        lines.extend(["", "評論者摘要：" + "、".join(profile_bits)])
    if review.profile.url:
        lines.append("評論者頁面：" + review.profile.url)
    lines.extend(["店家連結：" + review.shop_url, f"照片：{len(review.photo_paths)} 張"])
    return "\n".join(lines)


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    total = len(chunks)
    return [f"[{i}/{total}]\n{part}" for i, part in enumerate(chunks, 1)]


def build_parts(
    event_type: str, review: ReviewSnapshot, timezone_name: str,
    first_seen_at: str | None = None, base_dir: Path | None = None,
) -> list[dict]:
    parts = [{"kind": "text", "text": text} for text in split_text(
        render_review(event_type, review, timezone_name, first_seen_at)
    )]
    paths = [str((base_dir / path).resolve()) if base_dir and not Path(path).is_absolute() else path for path in review.photo_paths]
    for start in range(0, len(paths), 10):
        parts.append({"kind": "media", "paths": paths[start:start + 10]})
    return parts


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout: float = 30):
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def verify(self) -> dict:
        response = self.client.get(f"{self.base}/getMe")
        return self._result(response)

    def verify_chat(self) -> dict:
        response = self.client.get(f"{self.base}/getChat", params={"chat_id": self.chat_id})
        return self._result(response)

    def send_part(self, part: dict) -> None:
        if part["kind"] == "text":
            response = self.client.post(f"{self.base}/sendMessage", data={"chat_id": self.chat_id, "text": part["text"]})
            self._result(response)
            return
        self._send_media([Path(item) for item in part["paths"]])

    def _send_media(self, paths: list[Path]) -> None:
        if len(paths) == 1:
            prepared, mime = prepare_photo(paths[0])
            response = self.client.post(
                f"{self.base}/sendPhoto", data={"chat_id": self.chat_id},
                files={"photo": (paths[0].stem + ".jpg", prepared, mime)},
            )
            self._result(response)
            return
        files: dict[str, tuple[str, bytes, str]] = {}
        media: list[dict] = []
        for index, path in enumerate(paths):
            prepared, mime = prepare_photo(path)
            key = f"photo{index}"
            files[key] = (path.stem + ".jpg", prepared, mime)
            media.append({"type": "photo", "media": f"attach://{key}"})
        response = self.client.post(
            f"{self.base}/sendMediaGroup", data={"chat_id": self.chat_id, "media": json.dumps(media)}, files=files
        )
        self._result(response)

    @staticmethod
    def _result(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Telegram 回傳非 JSON（HTTP {response.status_code}）") from exc
        if response.is_error or not payload.get("ok"):
            raise RuntimeError(f"Telegram API 失敗（HTTP {response.status_code}）：{payload.get('description', '未知錯誤')}")
        return payload["result"]


def prepare_photo(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    if len(raw) <= PHOTO_SAFE_BYTES and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        return raw, "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        image.thumbnail((4096, 4096))
        output = io.BytesIO()
        quality = 88
        image.save(output, "JPEG", quality=quality, optimize=True)
        while output.tell() > PHOTO_SAFE_BYTES and quality > 50:
            quality -= 10
            output = io.BytesIO()
            image.save(output, "JPEG", quality=quality, optimize=True)
        return output.getvalue(), "image/jpeg"


def deliver_due(
    db: Database,
    client: TelegramClient,
    limit: int = 15,
    delay_min_seconds: float = 3,
    delay_max_seconds: float = 7,
) -> tuple[int, int]:
    sent = failed = 0
    rows = db.due_parts(limit=limit)
    for index, row in enumerate(rows):
        if index:
            delay = random.uniform(delay_min_seconds, delay_max_seconds)
            LOG.info("下一則 Telegram 通知將等待 %.1f 秒", delay)
            time.sleep(delay)
        try:
            client.send_part(json.loads(row["payload_json"]))
            db.mark_part_sent(row["id"])
            sent += 1
        except Exception as exc:
            attempts = int(row["attempts"]) + 1
            db.mark_part_failed(row["id"], attempts, str(exc))
            LOG.error("Telegram 事件 %s 第 %s 部分發送失敗：%s", row["event_key"], row["part_no"], exc)
            failed += 1
    return sent, failed
