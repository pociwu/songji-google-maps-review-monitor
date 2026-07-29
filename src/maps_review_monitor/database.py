from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .models import ReviewSnapshot
from .review_times import back_calculate, estimate_from_transition, parse_relative_time


SCHEMA_VERSION = 2


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS shops (
  shop_key TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
  initialized INTEGER NOT NULL DEFAULT 0, consecutive_failures INTEGER NOT NULL DEFAULT 0,
  alert_active INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT, last_success_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  shop_key TEXT NOT NULL, review_id TEXT NOT NULL, snapshot_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, reply_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  back_calculated_at TEXT, estimated_posted_date TEXT,
  time_parse_status TEXT NOT NULL DEFAULT 'pending',
  PRIMARY KEY (shop_key, review_id), FOREIGN KEY(shop_key) REFERENCES shops(shop_key)
);
CREATE TABLE IF NOT EXISTS review_time_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_key TEXT NOT NULL, review_id TEXT NOT NULL, time_text TEXT NOT NULL,
  relative_unit TEXT, relative_value INTEGER,
  first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
  FOREIGN KEY(shop_key,review_id) REFERENCES reviews(shop_key,review_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_review_time_latest
  ON review_time_observations(shop_key,review_id,id DESC);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL, shop_key TEXT, review_id TEXT,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_parts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
  part_no INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL, sent_at TEXT, last_error TEXT,
  UNIQUE(event_id, part_no), FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notification_due ON notification_parts(status, next_attempt_at, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path, timezone_name: str = "Asia/Taipei"):
        self.path = path
        self.timezone_name = timezone_name
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists() and path.stat().st_size > 0
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if existed and self._needs_migration():
            self._backup_before_migration()
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _needs_migration(self) -> bool:
        table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews'"
        ).fetchone()
        if not table:
            return False
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(reviews)")}
        shop_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(shops)")}
        return (
            int(self.conn.execute("PRAGMA user_version").fetchone()[0]) < SCHEMA_VERSION
            or "back_calculated_at" not in columns
            or "estimated_posted_date" not in columns
            or "time_parse_status" not in columns
            or "last_success_at" not in shop_columns
        )

    def _backup_before_migration(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        number = 1
        while backup.exists():
            backup = self.path.with_name(f"{self.path.name}.bak-{stamp}-{number}")
            number += 1
        target = sqlite3.connect(backup)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return backup

    def _migrate(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(reviews)")}
        additions = {
            "back_calculated_at": "TEXT",
            "estimated_posted_date": "TEXT",
            "time_parse_status": "TEXT NOT NULL DEFAULT 'pending'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE reviews ADD COLUMN {name} {definition}")
        shop_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(shops)")}
        if "last_success_at" not in shop_columns:
            self.conn.execute("ALTER TABLE shops ADD COLUMN last_success_at TEXT")
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def sync_shop(self, shop_key: str, name: str, url: str) -> None:
        now = utcnow()
        self.conn.execute(
            """INSERT INTO shops(shop_key,name,url,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(shop_key) DO UPDATE SET name=excluded.name,url=excluded.url,updated_at=excluded.updated_at""",
            (shop_key, name, url, now),
        )
        self.conn.commit()

    def is_initialized(self, shop_key: str) -> bool:
        row = self.conn.execute("SELECT initialized FROM shops WHERE shop_key=?", (shop_key,)).fetchone()
        return bool(row and row[0])

    def has_review(self, shop_key: str, review_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM reviews WHERE shop_key=? AND review_id=?", (shop_key, review_id)
        ).fetchone() is not None

    def get_review(self, shop_key: str, review_id: str) -> ReviewSnapshot | None:
        row = self.conn.execute(
            "SELECT snapshot_json FROM reviews WHERE shop_key=? AND review_id=?", (shop_key, review_id)
        ).fetchone()
        return ReviewSnapshot.from_dict(json.loads(row[0])) if row else None

    def review_first_seen(self, shop_key: str, review_id: str) -> str:
        row = self.conn.execute(
            "SELECT first_seen_at FROM reviews WHERE shop_key=? AND review_id=?", (shop_key, review_id)
        ).fetchone()
        if not row:
            raise KeyError((shop_key, review_id))
        return str(row[0])

    def upsert_review(
        self, review: ReviewSnapshot, notify: bool, observed_at: str | None = None
    ) -> list[str]:
        now = observed_at or utcnow()
        row = self.conn.execute(
            """SELECT content_hash,reply_hash,back_calculated_at,
                      estimated_posted_date,time_parse_status
               FROM reviews WHERE shop_key=? AND review_id=?""",
            (review.shop_key, review.review_id),
        ).fetchone()
        content_hash, reply_hash = review.content_hash(), review.reply_hash()
        relative = parse_relative_time(review.time_text)
        status = relative.kind if relative else "unparsed"
        back_at = str(row["back_calculated_at"] or "") if row else ""
        estimated_date = str(row["estimated_posted_date"] or "") if row else ""
        if not back_at and relative and status == "short":
            back_at = back_calculate(relative, now, self.timezone_name) or ""
        review.back_calculated_at = back_at
        review.estimated_posted_date = estimated_date
        review.time_parse_status = status
        event_types: list[str] = []
        if row is None:
            if notify:
                event_types.append("review_new")
            self.conn.execute(
                """INSERT INTO reviews(
                       shop_key,review_id,snapshot_json,content_hash,reply_hash,
                       first_seen_at,updated_at,back_calculated_at,
                       estimated_posted_date,time_parse_status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    review.shop_key, review.review_id, _json(review.to_dict()),
                    content_hash, reply_hash, now, now, back_at or None,
                    estimated_date or None, status,
                ),
            )
        else:
            if notify and row["content_hash"] != content_hash:
                event_types.append("review_updated")
            if notify and row["reply_hash"] != reply_hash:
                event_types.append("owner_reply_added" if review.owner_reply else "owner_reply_removed")
        estimated_date = self._record_time_observation(review, now, relative, estimated_date)
        review.estimated_posted_date = estimated_date
        self.conn.execute(
            """UPDATE reviews
               SET snapshot_json=?,content_hash=?,reply_hash=?,updated_at=?,
                   back_calculated_at=?,estimated_posted_date=?,time_parse_status=?
               WHERE shop_key=? AND review_id=?""",
            (
                _json(review.to_dict()), content_hash, reply_hash, now,
                back_at or None, estimated_date or None, status,
                review.shop_key, review.review_id,
            ),
        )
        self.conn.commit()
        return event_types

    def _record_time_observation(
        self, review: ReviewSnapshot, observed_at: str, relative, estimated_date: str
    ) -> str:
        latest = self.conn.execute(
            """SELECT * FROM review_time_observations
               WHERE shop_key=? AND review_id=? ORDER BY id DESC LIMIT 1""",
            (review.shop_key, review.review_id),
        ).fetchone()
        unit = relative.unit if relative else None
        value = relative.value if relative else None
        same_value = bool(
            latest
            and latest["relative_unit"] == unit
            and latest["relative_value"] == value
            and (relative is not None or latest["time_text"] == review.time_text)
        )
        if same_value:
            self.conn.execute(
                "UPDATE review_time_observations SET time_text=?,last_observed_at=? WHERE id=?",
                (review.time_text, observed_at, latest["id"]),
            )
            return estimated_date

        if (
            not estimated_date
            and not review.back_calculated_at
            and latest
            and relative
            and relative.kind == "long"
            and latest["relative_unit"] == relative.unit
            and latest["relative_value"] is not None
            and relative.value > int(latest["relative_value"])
        ):
            estimated_date = estimate_from_transition(
                latest["last_observed_at"], observed_at, relative, self.timezone_name
            ) or ""

        self.conn.execute(
            """INSERT INTO review_time_observations(
                   shop_key,review_id,time_text,relative_unit,relative_value,
                   first_observed_at,last_observed_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                review.shop_key, review.review_id, review.time_text, unit, value,
                observed_at, observed_at,
            ),
        )
        return estimated_date

    def mark_shop_success(self, shop_key: str) -> bool:
        row = self.conn.execute("SELECT alert_active FROM shops WHERE shop_key=?", (shop_key,)).fetchone()
        recovered = bool(row and row[0])
        self.conn.execute(
            """UPDATE shops SET initialized=1,consecutive_failures=0,alert_active=0,
               last_checked_at=?,last_success_at=?,updated_at=? WHERE shop_key=?""",
            (utcnow(), utcnow(), utcnow(), shop_key),
        )
        self.conn.commit()
        return recovered

    def mark_shop_failure(self, shop_key: str) -> tuple[int, bool]:
        self.conn.execute(
            "UPDATE shops SET consecutive_failures=consecutive_failures+1,last_checked_at=?,updated_at=? WHERE shop_key=?",
            (utcnow(), utcnow(), shop_key),
        )
        row = self.conn.execute(
            "SELECT consecutive_failures,alert_active FROM shops WHERE shop_key=?", (shop_key,)
        ).fetchone()
        should_alert = bool(row[0] >= 3 and not row[1])
        if should_alert:
            self.conn.execute("UPDATE shops SET alert_active=1 WHERE shop_key=?", (shop_key,))
        self.conn.commit()
        return int(row[0]), should_alert

    def add_event(self, event_key: str, event_type: str, payload: dict[str, Any], parts: list[dict[str, Any]]) -> bool:
        try:
            with self.transaction():
                cur = self.conn.execute(
                    "INSERT INTO events(event_key,event_type,shop_key,review_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                    (event_key, event_type, payload.get("shop_key"), payload.get("review_id"), _json(payload), utcnow()),
                )
                event_id = cur.lastrowid
                for number, part in enumerate(parts):
                    self.conn.execute(
                        "INSERT INTO notification_parts(event_id,part_no,kind,payload_json,next_attempt_at) VALUES(?,?,?,?,?)",
                        (event_id, number, part["kind"], _json(part), utcnow()),
                    )
            return True
        except sqlite3.IntegrityError:
            return False

    def due_parts(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT np.*,e.event_key FROM notification_parts np JOIN events e ON e.id=np.event_id
            WHERE np.status='pending' AND np.next_attempt_at<=? ORDER BY np.id LIMIT ?""",
            (utcnow(), limit),
        ))

    def mark_part_sent(self, part_id: int) -> None:
        self.conn.execute(
            "UPDATE notification_parts SET status='sent',sent_at=?,last_error=NULL WHERE id=?", (utcnow(), part_id)
        )
        self.conn.commit()

    def mark_part_failed(self, part_id: int, attempts: int, error: str) -> None:
        delay = min(3600, 30 * (2 ** min(attempts, 7)))
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        self.conn.execute(
            "UPDATE notification_parts SET attempts=?,next_attempt_at=?,last_error=? WHERE id=?",
            (attempts, next_at, error[:1000], part_id),
        )
        self.conn.commit()

    def iter_reviews(self, shop_key: str | None = None) -> Iterator[dict[str, Any]]:
        sql = """SELECT snapshot_json,first_seen_at,updated_at,
                        back_calculated_at,estimated_posted_date,time_parse_status
                 FROM reviews"""
        params: tuple[Any, ...] = ()
        if shop_key:
            sql += " WHERE shop_key=?"
            params = (shop_key,)
        sql += " ORDER BY first_seen_at DESC"
        for row in self.conn.execute(sql, params):
            item = json.loads(row["snapshot_json"])
            item["first_seen_at"] = row["first_seen_at"]
            item["updated_at"] = row["updated_at"]
            item["back_calculated_at"] = row["back_calculated_at"]
            item["estimated_posted_date"] = row["estimated_posted_date"]
            item["time_parse_status"] = row["time_parse_status"]
            yield item

    def iter_shop_statuses(self) -> Iterator[dict[str, Any]]:
        """Return the public, non-sensitive monitoring state for each known shop."""
        rows = self.conn.execute(
            """SELECT s.shop_key,s.name,s.url,s.initialized,s.consecutive_failures,
                      s.last_checked_at,s.last_success_at,
                      MAX(r.first_seen_at) AS latest_review_at
               FROM shops s
               LEFT JOIN reviews r ON r.shop_key=s.shop_key
               GROUP BY s.shop_key
               ORDER BY s.name COLLATE NOCASE"""
        )
        for row in rows:
            failures = int(row["consecutive_failures"])
            yield {
                "shop_key": row["shop_key"],
                "name": row["name"],
                "url": row["url"],
                "initialized": bool(row["initialized"]),
                "consecutive_failures": failures,
                "last_checked_at": row["last_checked_at"],
                "last_success_at": row["last_success_at"],
                "latest_review_at": row["latest_review_at"],
                "last_result": "failure" if failures else ("success" if row["initialized"] else "pending"),
            }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
