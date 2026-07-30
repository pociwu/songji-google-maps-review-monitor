from __future__ import annotations

import logging
import random
import time

from .config import Settings
from .database import Database, utcnow
from .models import ReviewSnapshot
from .notifications import TelegramClient, build_parts, deliver_due, split_text
from .scraper import MapsScraper

LOG = logging.getLogger(__name__)


def enqueue_review(
    db: Database, event_type: str, review: ReviewSnapshot, timezone_name: str, base_dir
) -> bool:
    digest = review.reply_hash() if event_type.startswith("owner_reply") else review.content_hash()
    event_key = f"{event_type}:{review.shop_key}:{review.review_id}:{digest}"
    first_seen = db.review_first_seen(review.shop_key, review.review_id)
    return db.add_event(
        event_key, event_type, review.to_dict(),
        build_parts(event_type, review, timezone_name, first_seen, base_dir),
    )


def enqueue_system(db: Database, event_key: str, event_type: str, shop_key: str, text: str) -> bool:
    return db.add_event(
        event_key, event_type, {"shop_key": shop_key},
        [{"kind": "text", "text": part} for part in split_text(text)],
    )


def run_check(settings: Settings, baseline_only: bool = False) -> dict[str, int]:
    stats = {
        "shops_ok": 0, "shops_failed": 0, "reviews": 0, "events": 0,
        "sent": 0, "send_failed": 0, "analysis_ok": 0, "analysis_failed": 0,
    }
    db = Database(settings.database_path, settings.timezone)
    telegram = None
    telegram_budget = settings.telegram_batch_limit
    try:
        for shop in settings.shops:
            db.sync_shop(shop.key, shop.name, shop.url)
        if not baseline_only:
            telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
            sent, failed = deliver_due(
                db, telegram, telegram_budget,
                settings.telegram_send_delay_min_seconds,
                settings.telegram_send_delay_max_seconds,
            )
            stats["sent"] += sent
            stats["send_failed"] += failed
            telegram_budget -= sent + failed
        with MapsScraper(settings) as scraper:
            for index, shop in enumerate(settings.shops):
                initialized = db.is_initialized(shop.key)
                notify = initialized and not baseline_only
                try:
                    reviews = scraper.scrape_shop(shop, lambda review_id: db.get_review(shop.key, review_id))
                    for review in reviews:
                        events = db.upsert_review(review, notify=notify)
                        stats["reviews"] += 1
                        for event_type in events:
                            if enqueue_review(db, event_type, review, settings.timezone, settings.root):
                                stats["events"] += 1
                    recovered = db.mark_shop_success(shop.key)
                    if recovered and notify:
                        if enqueue_system(
                            db, f"recovered:{shop.key}:{utcnow()}", "shop_recovered", shop.key,
                            f"✅ Google Maps 監控已恢復\n店家：{shop.name}\n連結：{shop.url}",
                        ):
                            stats["events"] += 1
                    stats["shops_ok"] += 1
                except Exception as exc:
                    count, should_alert = db.mark_shop_failure(shop.key)
                    stats["shops_failed"] += 1
                    LOG.exception("店家 %s 抓取失敗（連續 %s 次）", shop.name, count)
                    if should_alert and not baseline_only:
                        if enqueue_system(
                            db, f"failure:{shop.key}:{count}:{utcnow()}", "shop_failure", shop.key,
                            f"⚠️ Google Maps 監控連續失敗 {count} 次\n店家：{shop.name}\n原因：{exc}\n除錯資料：{settings.debug_dir}",
                        ):
                            stats["events"] += 1
                if index < len(settings.shops) - 1:
                    delay = random.uniform(
                        settings.shop_delay_min_seconds, settings.shop_delay_max_seconds
                    )
                    LOG.info("下一家店將等待 %.1f 秒", delay)
                    time.sleep(delay)
        if telegram is not None and telegram_budget > 0:
            sent, failed = deliver_due(
                db, telegram, telegram_budget,
                settings.telegram_send_delay_min_seconds,
                settings.telegram_send_delay_max_seconds,
            )
            stats["sent"] += sent
            stats["send_failed"] += failed
        if settings.analysis_enabled:
            try:
                from .analysis import run_analysis

                run_analysis(settings)
                stats["analysis_ok"] = 1
            except Exception:
                stats["analysis_failed"] = 1
                LOG.exception("評論相似度分析失敗；監控與通知結果不受影響")
        return stats
    finally:
        if telegram:
            telegram.close()
        db.close()
