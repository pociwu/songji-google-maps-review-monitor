from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any, Iterator
from uuid import uuid4

from .evidence_inputs import (
    analysis_input_matches,
    canonical_contributor_id,
)
from .models import ReviewSnapshot
from .review_times import back_calculate, estimate_from_transition, parse_relative_time


SCHEMA_VERSION = 6
NOTIFICATION_TTL = timedelta(days=7)
NOTIFICATION_MAX_ATTEMPTS = 8
NOTIFICATION_CLAIM_LEASE = timedelta(minutes=5)


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
CREATE TABLE IF NOT EXISTS shop_overview_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_key TEXT NOT NULL REFERENCES shops(shop_key), captured_at TEXT NOT NULL,
  snapshot_json TEXT NOT NULL, changes_json TEXT NOT NULL, observed_fields_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_overview_history ON shop_overview_versions(shop_key,id DESC);
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
  next_attempt_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  claim_token TEXT, claimed_at TEXT, claim_expires_at TEXT,
  sent_at TEXT, last_error TEXT,
  UNIQUE(event_id, part_no), FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notification_due ON notification_parts(status, next_attempt_at, id);
CREATE TABLE IF NOT EXISTS review_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_key TEXT NOT NULL, review_id TEXT NOT NULL,
  content_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  UNIQUE(shop_key,review_id,content_hash)
);
CREATE TABLE IF NOT EXISTS analysis_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL, stage TEXT NOT NULL, percent INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
  model_version TEXT NOT NULL, settings_json TEXT NOT NULL,
  started_at TEXT NOT NULL, completed_at TEXT, error TEXT,
  is_current INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_current
  ON analysis_runs(is_current,status,id DESC);
CREATE TABLE IF NOT EXISTS analysis_reviews (
  run_id INTEGER NOT NULL, shop_key TEXT NOT NULL, review_id TEXT NOT NULL,
  text_hash TEXT NOT NULL, embedding BLOB,
  label TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '',
  max_lexical REAL NOT NULL DEFAULT 0, max_semantic REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id,shop_key,review_id),
  FOREIGN KEY(run_id) REFERENCES analysis_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS analysis_groups (
  run_id INTEGER NOT NULL, group_id TEXT NOT NULL, scope TEXT NOT NULL,
  fingerprint TEXT NOT NULL, label TEXT NOT NULL, direction TEXT NOT NULL,
  review_count INTEGER NOT NULL, shop_count INTEGER NOT NULL,
  max_lexical REAL NOT NULL, max_semantic REAL NOT NULL,
  latest_at TEXT, evidence_json TEXT NOT NULL, excluded_reason TEXT,
  PRIMARY KEY(run_id,group_id),
  FOREIGN KEY(run_id) REFERENCES analysis_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_analysis_groups_scope
  ON analysis_groups(run_id,scope,label,direction);
CREATE TABLE IF NOT EXISTS analysis_group_reviews (
  run_id INTEGER NOT NULL, group_id TEXT NOT NULL,
  shop_key TEXT NOT NULL, review_id TEXT NOT NULL,
  max_lexical REAL NOT NULL DEFAULT 0, max_semantic REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id,group_id,shop_key,review_id),
  FOREIGN KEY(run_id,group_id) REFERENCES analysis_groups(run_id,group_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS analysis_pairs (
  run_id INTEGER NOT NULL, scope TEXT NOT NULL,
  left_shop_key TEXT NOT NULL, left_review_id TEXT NOT NULL,
  right_shop_key TEXT NOT NULL, right_review_id TEXT NOT NULL,
  lexical REAL NOT NULL, semantic REAL NOT NULL,
  PRIMARY KEY(run_id,scope,left_shop_key,left_review_id,right_shop_key,right_review_id),
  FOREIGN KEY(run_id) REFERENCES analysis_runs(id) ON DELETE CASCADE
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(
        self,
        path: Path,
        timezone_name: str = "Asia/Taipei",
        project_root: Path | None = None,
    ):
        self.path = path
        self.timezone_name = timezone_name
        inferred_root = path.parent.parent if path.parent.name == "data" else path.parent
        self.project_root = (project_root or inferred_root).resolve()
        self.media_root = (path.parent / "media").resolve()
        self._analysis_group_coverage: dict[int, dict[str, dict[str, int]]] = {}
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
        columns = (
            {row[1] for row in self.conn.execute("PRAGMA table_info(reviews)")}
            if table else set()
        )
        shop_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shops'"
        ).fetchone()
        shop_columns = (
            {row[1] for row in self.conn.execute("PRAGMA table_info(shops)")}
            if shop_table else set()
        )
        notification_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_parts'"
        ).fetchone()
        notification_columns = (
            {row[1] for row in self.conn.execute("PRAGMA table_info(notification_parts)")}
            if notification_table else set()
        )
        notification_expiry_missing = bool(
            notification_table
            and "expires_at" in notification_columns
            and self.conn.execute(
                """SELECT 1 FROM notification_parts
                   WHERE expires_at IS NULL OR expires_at='' LIMIT 1"""
            ).fetchone()
        )
        return (
            int(self.conn.execute("PRAGMA user_version").fetchone()[0]) < SCHEMA_VERSION
            or bool(table and "back_calculated_at" not in columns)
            or bool(table and "estimated_posted_date" not in columns)
            or bool(table and "time_parse_status" not in columns)
            or bool(shop_table and "last_success_at" not in shop_columns)
            or bool(notification_table and "expires_at" not in notification_columns)
            or bool(
                notification_table
                and not {"claim_token", "claimed_at", "claim_expires_at"}
                <= notification_columns
            )
            or notification_expiry_missing
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
        notification_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(notification_parts)")
        }
        if "expires_at" not in notification_columns:
            # SQLite cannot add a NOT NULL column with a dynamic timestamp
            # default. Add it nullable, then deterministically backfill every
            # legacy part from its event creation time before enabling v4.
            self.conn.execute("ALTER TABLE notification_parts ADD COLUMN expires_at TEXT")
        claim_additions = {
            "claim_token": "TEXT",
            "claimed_at": "TEXT",
            "claim_expires_at": "TEXT",
        }
        for name, definition in claim_additions.items():
            if name not in notification_columns:
                self.conn.execute(
                    f"ALTER TABLE notification_parts ADD COLUMN {name} {definition}"
                )
        legacy_parts = self.conn.execute(
            """SELECT np.id,e.created_at
               FROM notification_parts np
               JOIN events e ON e.id=np.event_id
               WHERE np.expires_at IS NULL OR np.expires_at=''"""
        ).fetchall()
        for part in legacy_parts:
            try:
                created_at = datetime.fromisoformat(str(part["created_at"]))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                # A corrupt/unknown legacy creation time must not revive an old
                # backlog for another seven days.
                created_at = datetime.fromtimestamp(0, timezone.utc)
            expires_at = (created_at.astimezone(timezone.utc) + NOTIFICATION_TTL).isoformat()
            self.conn.execute(
                "UPDATE notification_parts SET expires_at=? WHERE id=?",
                (expires_at, part["id"]),
            )
        # A legacy database may contain orphaned parts from a time when foreign
        # keys were not enabled.  The JOIN above cannot recover their event
        # timestamp; give them a deterministic, already-expired value so every
        # subsequent startup does not mistake the same corruption for a new
        # migration and create another backup.
        orphan_expiry = (
            datetime.fromtimestamp(0, timezone.utc) + NOTIFICATION_TTL
        ).isoformat()
        self.conn.execute(
            """UPDATE notification_parts SET expires_at=?
               WHERE expires_at IS NULL OR expires_at=''""",
            (orphan_expiry,),
        )
        now = utcnow()
        self._terminalize_notification_parts(now)
        self._recover_expired_notification_claims(now)
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
            """SELECT content_hash,reply_hash,back_calculated_at,snapshot_json,
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
            previous = ReviewSnapshot.from_dict(json.loads(row["snapshot_json"]))
            content_changed = previous.content_hash() != content_hash
            if content_changed:
                self.conn.execute(
                    """INSERT OR IGNORE INTO review_versions(
                           shop_key,review_id,content_hash,snapshot_json,captured_at
                       ) VALUES(?,?,?,?,?)""",
                    (review.shop_key, review.review_id, row["content_hash"], row["snapshot_json"], now),
                )
            if notify and content_changed:
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
        created_at = utcnow()
        expires_at = (
            datetime.fromisoformat(created_at) + NOTIFICATION_TTL
        ).isoformat()
        try:
            with self.transaction():
                cur = self.conn.execute(
                    "INSERT INTO events(event_key,event_type,shop_key,review_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                    (event_key, event_type, payload.get("shop_key"), payload.get("review_id"), _json(payload), created_at),
                )
                event_id = cur.lastrowid
                for number, part in enumerate(parts):
                    self.conn.execute(
                        """INSERT INTO notification_parts(
                               event_id,part_no,kind,payload_json,next_attempt_at,expires_at
                           ) VALUES(?,?,?,?,?,?)""",
                        (event_id, number, part["kind"], _json(part), created_at, expires_at),
                    )
            return True
        except sqlite3.IntegrityError:
            return False

    def due_parts(self, limit: int = 100) -> list[sqlite3.Row]:
        now = utcnow()
        with self.transaction():
            self._terminalize_notification_parts(now)
            self._recover_expired_notification_claims(now)
            rows = list(self.conn.execute(
                """SELECT np.*,e.event_key
                   FROM notification_parts np JOIN events e ON e.id=np.event_id
                   WHERE np.status='pending' AND np.attempts<?
                     AND julianday(np.expires_at)>julianday(?)
                     AND julianday(np.next_attempt_at)<=julianday(?)
                   ORDER BY np.id LIMIT ?""",
                (NOTIFICATION_MAX_ATTEMPTS, now, now, limit),
            ))
        return rows

    def claim_due_parts(
        self, limit: int = 100, lease_seconds: float | None = None
    ) -> list[sqlite3.Row]:
        """Atomically reserve due parts for exactly one delivery worker.

        SQLite's BEGIN IMMEDIATE transaction serializes the select-and-update
        sequence across processes. A token protects a recovered/reclaimed part
        from a late completion callback belonging to the former worker.
        """
        now = utcnow()
        lease = (
            NOTIFICATION_CLAIM_LEASE
            if lease_seconds is None
            else timedelta(seconds=max(1.0, lease_seconds))
        )
        claim_expires_at = (datetime.fromisoformat(now) + lease).isoformat()
        token = uuid4().hex
        with self.transaction():
            self._terminalize_notification_parts(now)
            self._recover_expired_notification_claims(now)
            due_ids = [
                int(row["id"])
                for row in self.conn.execute(
                    """SELECT id FROM notification_parts
                       WHERE status='pending' AND attempts<?
                         AND julianday(expires_at)>julianday(?)
                         AND julianday(next_attempt_at)<=julianday(?)
                       ORDER BY id LIMIT ?""",
                    (NOTIFICATION_MAX_ATTEMPTS, now, now, limit),
                )
            ]
            if not due_ids:
                return []
            placeholders = ",".join("?" for _ in due_ids)
            self.conn.execute(
                f"""UPDATE notification_parts
                    SET status='sending',claim_token=?,claimed_at=?,claim_expires_at=?
                    WHERE status='pending' AND id IN ({placeholders})""",
                (token, now, claim_expires_at, *due_ids),
            )
            rows = list(self.conn.execute(
                f"""SELECT np.*,e.event_key
                    FROM notification_parts np
                    JOIN events e ON e.id=np.event_id
                    WHERE np.claim_token=? AND np.id IN ({placeholders})
                    ORDER BY np.id""",
                (token, *due_ids),
            ))
        return rows

    def claim_is_sendable(self, part_id: int, claim_token: str) -> bool:
        """Recheck ownership, event TTL and lease immediately before I/O."""
        now = utcnow()
        with self.transaction():
            self._terminalize_notification_parts(now)
            self._recover_expired_notification_claims(now)
            return self.conn.execute(
                """SELECT 1 FROM notification_parts
                   WHERE id=? AND status='sending' AND claim_token=?
                     AND julianday(expires_at)>julianday(?)
                     AND julianday(claim_expires_at)>julianday(?)""",
                (part_id, claim_token, now, now),
            ).fetchone() is not None

    def _terminalize_notification_parts(self, now: str) -> None:
        self.conn.execute(
            """UPDATE notification_parts
               SET status='dead',last_error=COALESCE(
                   last_error,'已達 Telegram 通知最大重試次數'
               ),claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL
               WHERE status IN ('pending','sending') AND attempts>=?""",
            (NOTIFICATION_MAX_ATTEMPTS,),
        )
        self.conn.execute(
            """UPDATE notification_parts
               SET status='expired',last_error=COALESCE(
                   last_error,'Telegram 通知已超過 7 日有效期限'
               ),claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL
               WHERE status IN ('pending','sending') AND (
                   expires_at IS NULL OR julianday(expires_at) IS NULL
                   OR julianday(expires_at)<=julianday(?)
               )""",
            (now,),
        )

    def _recover_expired_notification_claims(self, now: str) -> None:
        self.conn.execute(
            """UPDATE notification_parts
               SET status='pending',next_attempt_at=?,claim_token=NULL,
                   claimed_at=NULL,claim_expires_at=NULL,
                   last_error=COALESCE(
                       last_error,'Telegram 發送程序中斷，通知已重新排隊'
                   )
               WHERE status='sending' AND (
                   claim_token IS NULL OR claim_expires_at IS NULL
                   OR julianday(claim_expires_at) IS NULL
                   OR julianday(claim_expires_at)<=julianday(?)
               )""",
            (now, now),
        )

    def mark_part_sent(self, part_id: int, claim_token: str | None = None) -> bool:
        if claim_token is None:
            cursor = self.conn.execute(
                """UPDATE notification_parts
                   SET status='sent',sent_at=?,last_error=NULL,
                       claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL
                   WHERE id=? AND status IN ('pending','sending')""",
                (utcnow(), part_id),
            )
        else:
            cursor = self.conn.execute(
                """UPDATE notification_parts
                   SET status='sent',sent_at=?,last_error=NULL,
                       claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL
                   WHERE id=? AND status='sending' AND claim_token=?""",
                (utcnow(), part_id, claim_token),
            )
        self.conn.commit()
        return cursor.rowcount == 1

    def mark_part_failed(
        self, part_id: int, attempts: int, error: str,
        claim_token: str | None = None,
    ) -> bool:
        claim_clause = (
            "id=? AND status IN ('pending','sending')"
            if claim_token is None
            else "id=? AND status='sending' AND claim_token=?"
        )
        claim_params: tuple[Any, ...] = (
            (part_id,) if claim_token is None else (part_id, claim_token)
        )
        if attempts >= NOTIFICATION_MAX_ATTEMPTS:
            cursor = self.conn.execute(
                f"""UPDATE notification_parts
                    SET status='dead',attempts=?,last_error=?,claim_token=NULL,
                        claimed_at=NULL,claim_expires_at=NULL
                    WHERE {claim_clause}""",
                (attempts, error[:1000], *claim_params),
            )
            self.conn.commit()
            return cursor.rowcount == 1
        delay = min(3600, 30 * (2 ** min(attempts, 7)))
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        cursor = self.conn.execute(
            f"""UPDATE notification_parts
                SET status='pending',attempts=?,next_attempt_at=?,last_error=?,
                    claim_token=NULL,claimed_at=NULL,claim_expires_at=NULL
                WHERE {claim_clause}""",
            (attempts, next_at, error[:1000], *claim_params),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def iter_reviews(self, shop_key: str | None = None) -> Iterator[dict[str, Any]]:
        sql = """SELECT r.snapshot_json,r.first_seen_at,r.updated_at,
                        r.back_calculated_at,r.estimated_posted_date,r.time_parse_status,
                        (
                          SELECT o.relative_unit FROM review_time_observations o
                          WHERE o.shop_key=r.shop_key AND o.review_id=r.review_id
                            AND o.relative_unit IS NOT NULL
                          ORDER BY o.id ASC LIMIT 1
                        ) AS time_relative_unit,
                        EXISTS(
                          SELECT 1 FROM review_versions rv
                          WHERE rv.shop_key=r.shop_key AND rv.review_id=r.review_id
                        ) AS edited
                 FROM reviews r"""
        params: tuple[Any, ...] = ()
        if shop_key:
            sql += " WHERE r.shop_key=?"
            params = (shop_key,)
        sql += " ORDER BY first_seen_at DESC"
        for row in self.conn.execute(sql, params):
            item = json.loads(row["snapshot_json"])
            item["first_seen_at"] = row["first_seen_at"]
            item["updated_at"] = row["updated_at"]
            item["back_calculated_at"] = row["back_calculated_at"]
            item["estimated_posted_date"] = row["estimated_posted_date"]
            item["time_parse_status"] = row["time_parse_status"]
            item["time_relative_unit"] = row["time_relative_unit"]
            item["edited"] = bool(row["edited"])
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

    def start_analysis(self, model_version: str, settings: dict[str, Any], total: int) -> int:
        self.conn.execute(
            """UPDATE analysis_runs SET status='failed',stage='執行中斷',
               completed_at=?,error='前次程序未正常結束'
               WHERE status='running'""",
            (utcnow(),),
        )
        cur = self.conn.execute(
            """INSERT INTO analysis_runs(
                   status,stage,percent,processed,total,model_version,settings_json,started_at
               ) VALUES('running','準備資料',0,0,?,?,?,?)""",
            (total, model_version, _json(settings), utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_analysis_progress(
        self, run_id: int, stage: str, percent: int, processed: int, total: int
    ) -> None:
        self.conn.execute(
            """UPDATE analysis_runs SET stage=?,percent=?,processed=?,total=?
               WHERE id=? AND status='running'""",
            (stage, max(0, min(100, percent)), processed, total, run_id),
        )
        self.conn.commit()

    def fail_analysis(self, run_id: int, error: str) -> None:
        self.conn.execute(
            """UPDATE analysis_runs SET status='failed',stage='分析失敗',
               completed_at=?,error=? WHERE id=?""",
            (utcnow(), error[:2000], run_id),
        )
        self.conn.commit()

    def complete_analysis(
        self,
        run_id: int,
        reviews: list[dict[str, Any]],
        groups: list[dict[str, Any]],
        pairs: list[dict[str, Any]],
    ) -> None:
        with self.transaction():
            for item in reviews:
                self.conn.execute(
                    """INSERT INTO analysis_reviews(
                           run_id,shop_key,review_id,text_hash,embedding,label,direction,
                           max_lexical,max_semantic
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, item["shop_key"], item["review_id"], item["text_hash"],
                        item.get("embedding"), item.get("label", ""), item.get("direction", ""),
                        item.get("max_lexical", 0), item.get("max_semantic", 0),
                    ),
                )
            for group in groups:
                self.conn.execute(
                    """INSERT INTO analysis_groups(
                           run_id,group_id,scope,fingerprint,label,direction,review_count,
                           shop_count,max_lexical,max_semantic,latest_at,evidence_json,excluded_reason
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, group["group_id"], group["scope"], group["fingerprint"],
                        group["label"], group["direction"], group["review_count"],
                        group["shop_count"], group["max_lexical"], group["max_semantic"],
                        group.get("latest_at"), _json(group.get("evidence", [])),
                        group.get("excluded_reason"),
                    ),
                )
                for member in group["members"]:
                    self.conn.execute(
                        """INSERT INTO analysis_group_reviews(
                               run_id,group_id,shop_key,review_id,max_lexical,max_semantic
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            run_id, group["group_id"], member["shop_key"], member["review_id"],
                            member.get("max_lexical", 0), member.get("max_semantic", 0),
                        ),
                    )
            for pair in pairs:
                self.conn.execute(
                    """INSERT INTO analysis_pairs VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        run_id, pair["scope"], pair["left_shop_key"], pair["left_review_id"],
                        pair["right_shop_key"], pair["right_review_id"],
                        pair["lexical"], pair["semantic"],
                    ),
                )
            self.conn.execute("UPDATE analysis_runs SET is_current=0 WHERE is_current=1")
            self.conn.execute(
                """UPDATE analysis_runs SET status='completed',stage='分析完成',
                   percent=100,processed=total,completed_at=?,is_current=1 WHERE id=?""",
                (utcnow(), run_id),
            )

    def analysis_status(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM analysis_runs
               ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,id DESC LIMIT 1"""
        ).fetchone()
        return dict(row) if row else None

    def current_analysis_run_id(self) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM analysis_runs WHERE is_current=1 AND status='completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None

    def analysis_shop_counts(self) -> dict[str, dict[str, int]]:
        run_id = self.current_analysis_run_id()
        if run_id is None:
            return {}
        valid_groups = self._valid_analysis_group_ids(run_id)
        if not valid_groups:
            return {}
        result: dict[str, dict[str, int]] = {}
        # A review may belong to several same/cross-shop groups. Count it once at
        # its strongest effective level so the two homepage counters are
        # mutually exclusive and match the shop filters.
        strongest: dict[tuple[str, str], str] = {}
        effective_labels: dict[str, str] = {}
        for row in self.conn.execute(
            """SELECT ag.group_id,ag.scope,ag.label,ag.evidence_json,
                      agr.shop_key,agr.review_id
               FROM analysis_groups ag JOIN analysis_group_reviews agr
                  ON agr.run_id=ag.run_id AND agr.group_id=ag.group_id
               WHERE ag.run_id=? AND ag.excluded_reason IS NULL""",
            (run_id,),
        ):
            if row["group_id"] in valid_groups:
                label = effective_labels.setdefault(
                    row["group_id"],
                    _effective_analysis_label(
                        row["label"],
                        row["evidence_json"],
                        row["scope"],
                        self._analysis_group_coverage.get(run_id, {}).get(
                            row["group_id"]
                        ),
                    ),
                )
                key = (row["shop_key"], row["review_id"])
                if label == "suspected" or key not in strongest:
                    strongest[key] = label
        for (shop_key, _review_id), label in strongest.items():
            result.setdefault(shop_key, {"similar": 0, "suspected": 0})
            result[shop_key]["suspected" if label == "suspected" else "similar"] += 1
        return result

    def review_analysis(self, shop_key: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        run_id = self.current_analysis_run_id()
        if run_id is None:
            return {}
        valid_groups = self._valid_analysis_group_ids(run_id)
        sql = """SELECT ar.*,r.snapshot_json,r.back_calculated_at,
                        r.estimated_posted_date,r.time_parse_status,
                        (
                          SELECT o.relative_unit FROM review_time_observations o
                          WHERE o.shop_key=r.shop_key AND o.review_id=r.review_id
                            AND o.relative_unit IS NOT NULL
                          ORDER BY o.id ASC LIMIT 1
                        ) AS time_relative_unit
                 FROM analysis_reviews ar
                 JOIN reviews r ON r.shop_key=ar.shop_key AND r.review_id=ar.review_id
                 WHERE ar.run_id=?"""
        params: list[Any] = [run_id]
        if shop_key:
            sql += " AND ar.shop_key=?"
            params.append(shop_key)
        result = {}
        for row in self.conn.execute(sql, params):
            current_review = _current_analysis_input(row)
            if not analysis_input_matches(
                row["text_hash"],
                current_review,
                project_root=self.project_root,
                media_root=self.media_root,
            ):
                continue
            item = dict(row)
            item.pop("snapshot_json", None)
            item.pop("back_calculated_at", None)
            item.pop("estimated_posted_date", None)
            item.pop("time_parse_status", None)
            item.pop("time_relative_unit", None)
            # Labels and scores are group-derived. Rebuild them from groups whose
            # complete member set still matches the current review text, otherwise
            # an edited sibling could leave a stale badge on this review.
            item.update(label="", direction="", max_lexical=0.0, max_semantic=0.0)
            result[(row["shop_key"], row["review_id"])] = item

        for row in self.conn.execute(
            """SELECT agr.group_id,agr.shop_key,agr.review_id,
                      agr.max_lexical,agr.max_semantic,ag.label,ag.direction,
                      ag.evidence_json,ag.scope
               FROM analysis_group_reviews agr
               JOIN analysis_groups ag
                 ON ag.run_id=agr.run_id AND ag.group_id=agr.group_id
               WHERE agr.run_id=? AND ag.excluded_reason IS NULL""",
            (run_id,),
        ):
            if row["group_id"] not in valid_groups:
                continue
            key = (row["shop_key"], row["review_id"])
            item = result.get(key)
            if item is None:
                continue
            effective_label = _effective_analysis_label(
                row["label"],
                row["evidence_json"],
                row["scope"],
                self._analysis_group_coverage.get(run_id, {}).get(row["group_id"]),
            )
            if effective_label == "suspected" or not item["label"]:
                item["label"] = effective_label
                item["direction"] = row["direction"]
            item["max_lexical"] = max(item["max_lexical"], row["max_lexical"])
            item["max_semantic"] = max(item["max_semantic"], row["max_semantic"])
        return result

    def analysis_groups(
        self, scope: str | None = None, shop_key: str | None = None
    ) -> list[dict[str, Any]]:
        run_id = self.current_analysis_run_id()
        if run_id is None:
            return []
        valid_groups = self._valid_analysis_group_ids(run_id)
        sql = """SELECT DISTINCT ag.* FROM analysis_groups ag
                 LEFT JOIN analysis_group_reviews agr
                   ON agr.run_id=ag.run_id AND agr.group_id=ag.group_id
                 WHERE ag.run_id=? AND ag.excluded_reason IS NULL"""
        params: list[Any] = [run_id]
        if scope:
            sql += " AND ag.scope=?"
            params.append(scope)
        if shop_key:
            sql += " AND agr.shop_key=?"
            params.append(shop_key)
        sql += " ORDER BY ag.review_count DESC,ag.max_semantic DESC,ag.latest_at DESC"
        result = []
        for row in self.conn.execute(sql, params):
            item = dict(row)
            if item["group_id"] not in valid_groups:
                continue
            raw_evidence = _load_json(item.pop("evidence_json"), [])
            item["evidence"] = _normalize_analysis_evidence(
                raw_evidence,
                item,
                coverage=self._analysis_group_coverage.get(run_id, {}).get(
                    item["group_id"]
                ),
            )
            item["label"] = _effective_label_for_evidence(item["evidence"])
            result.append(item)
        return result

    def analysis_group(self, group_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        run_id = self.current_analysis_run_id()
        if run_id is None:
            return None
        if group_id not in self._valid_analysis_group_ids(run_id):
            return None
        row = self.conn.execute(
            "SELECT * FROM analysis_groups WHERE run_id=? AND group_id=?",
            (run_id, group_id),
        ).fetchone()
        if not row:
            return None
        group = dict(row)
        raw_evidence = _load_json(group.pop("evidence_json"), [])
        members = []
        for member in self.conn.execute(
            """SELECT agr.max_lexical,agr.max_semantic,r.snapshot_json,
                       r.first_seen_at,r.back_calculated_at,r.estimated_posted_date,
                       r.time_parse_status,
                       (
                         SELECT o.relative_unit FROM review_time_observations o
                         WHERE o.shop_key=r.shop_key AND o.review_id=r.review_id
                           AND o.relative_unit IS NOT NULL
                         ORDER BY o.id ASC LIMIT 1
                       ) AS time_relative_unit
                FROM analysis_group_reviews agr
                JOIN reviews r ON r.shop_key=agr.shop_key AND r.review_id=agr.review_id
                WHERE agr.run_id=? AND agr.group_id=? ORDER BY r.first_seen_at DESC""",
            (run_id, group_id),
        ):
            item = json.loads(member["snapshot_json"])
            item.update(
                max_lexical=member["max_lexical"], max_semantic=member["max_semantic"],
                first_seen_at=member["first_seen_at"],
                back_calculated_at=member["back_calculated_at"],
                estimated_posted_date=member["estimated_posted_date"],
                time_parse_status=member["time_parse_status"],
                time_relative_unit=member["time_relative_unit"],
            )
            members.append(item)
        metrics, coverage = self._analysis_group_evidence_details(
            run_id, group_id, group["scope"], members
        )
        coverage.update(
            self._analysis_group_coverage.get(run_id, {}).get(group_id, {})
        )
        group["evidence"] = _normalize_analysis_evidence(
            raw_evidence, group, metrics=metrics, coverage=coverage
        )
        group["label"] = _effective_label_for_evidence(group["evidence"])
        return group, members

    def _analysis_group_evidence_details(
        self, run_id: int, group_id: str, scope: str, members: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Compute explainable group metrics from the immutable analysis snapshot."""
        # Probe only this group's possible pairs through the analysis_pairs
        # primary key.  A join led SQLite to start with every pair in the scope,
        # making one detail page scale with the entire analysis run.
        pair_rows: list[sqlite3.Row] = []
        possible_pairs = 0
        for index, left in enumerate(members):
            left_key = (str(left.get("shop_key", "")), str(left.get("review_id", "")))
            for right in members[index + 1 :]:
                right_key = (
                    str(right.get("shop_key", "")),
                    str(right.get("review_id", "")),
                )
                if scope == "cross" and left_key[0] == right_key[0]:
                    continue
                possible_pairs += 1
                pair = self.conn.execute(
                    """SELECT lexical,semantic FROM analysis_pairs
                       WHERE run_id=? AND scope=?
                         AND left_shop_key=? AND left_review_id=?
                         AND right_shop_key=? AND right_review_id=?""",
                    (run_id, scope, *left_key, *right_key),
                ).fetchone()
                if pair is None:
                    pair = self.conn.execute(
                        """SELECT lexical,semantic FROM analysis_pairs
                           WHERE run_id=? AND scope=?
                             AND left_shop_key=? AND left_review_id=?
                             AND right_shop_key=? AND right_review_id=?""",
                        (run_id, scope, *right_key, *left_key),
                    ).fetchone()
                if pair is not None:
                    pair_rows.append(pair)

        lexical = [float(row["lexical"]) for row in pair_rows]
        semantic = [float(row["semantic"]) for row in pair_rows]
        direct_pairs = len(pair_rows)

        metrics: dict[str, Any] = {
            "review_count": len(members),
            "shop_count": len({item.get("shop_key") for item in members}),
            "direct_pairs": direct_pairs,
            "possible_pairs": possible_pairs,
            "pair_density": direct_pairs / possible_pairs if possible_pairs else 0.0,
        }
        if lexical:
            metrics.update(
                min_lexical=min(lexical),
                median_lexical=median(lexical),
                max_lexical=max(lexical),
            )
        if semantic:
            metrics.update(
                min_semantic=min(semantic),
                median_semantic=median(semantic),
                max_semantic=max(semantic),
            )

        profile_urls = [
            str((item.get("profile") or {}).get("url", "")).strip()
            for item in members
        ]
        contributor_ids = [canonical_contributor_id(value) for value in profile_urls]
        known_dates = [
            item
            for item in members
            if item.get("back_calculated_at") or item.get("estimated_posted_date")
        ]
        precise_dates = [item for item in members if item.get("back_calculated_at")]
        coverage = {
            "profile_known_count": sum(bool(value) for value in contributor_ids),
            "distinct_profiles": len({value for value in contributor_ids if value}),
            "profile_total": len(members),
            "date_known_count": len(known_dates),
            "date_precise_count": len(precise_dates),
            "date_total": len(members),
        }
        return metrics, coverage

    def _valid_analysis_group_ids(self, run_id: int) -> set[str]:
        """Current pages never count a group whose analyzed reviewer text has since changed."""
        stale: set[str] = set()
        all_groups: set[str] = set()
        profile_ids: dict[str, set[str]] = defaultdict(set)
        profile_known: dict[str, int] = defaultdict(int)
        profile_total: dict[str, int] = defaultdict(int)
        v2_input_count: dict[str, int] = defaultdict(int)
        review_matches: dict[tuple[str, str], bool] = {}
        rows = self.conn.execute(
            """SELECT agr.group_id,agr.shop_key,agr.review_id,
                      ar.text_hash,r.snapshot_json,
                      r.back_calculated_at,r.estimated_posted_date,r.time_parse_status,
                      (
                        SELECT o.relative_unit FROM review_time_observations o
                        WHERE o.shop_key=r.shop_key AND o.review_id=r.review_id
                          AND o.relative_unit IS NOT NULL
                        ORDER BY o.id ASC LIMIT 1
                      ) AS time_relative_unit
               FROM analysis_group_reviews agr
               JOIN analysis_reviews ar
                 ON ar.run_id=agr.run_id AND ar.shop_key=agr.shop_key
                AND ar.review_id=agr.review_id
               JOIN reviews r ON r.shop_key=agr.shop_key AND r.review_id=agr.review_id
               WHERE agr.run_id=?""",
            (run_id,),
        )
        for row in rows:
            group_id = str(row["group_id"])
            all_groups.add(group_id)
            current = _current_analysis_input(row)
            contributor_id = canonical_contributor_id(
                str((current.get("profile") or {}).get("url", ""))
            )
            profile_total[group_id] += 1
            if str(row["text_hash"]).startswith("v2:"):
                v2_input_count[group_id] += 1
            if contributor_id:
                profile_known[group_id] += 1
                profile_ids[group_id].add(contributor_id)
            review_key = (str(row["shop_key"]), str(row["review_id"]))
            if review_key not in review_matches:
                review_matches[review_key] = analysis_input_matches(
                    row["text_hash"],
                    current,
                    project_root=self.project_root,
                    media_root=self.media_root,
                )
            if not review_matches[review_key]:
                stale.add(group_id)
        self._analysis_group_coverage[run_id] = {
            group_id: {
                "profile_known_count": profile_known[group_id],
                "profile_total": profile_total[group_id],
                "distinct_profiles": len(profile_ids[group_id]),
                "v2_input_count": v2_input_count[group_id],
            }
            for group_id in all_groups
        }
        return all_groups - stale


def _current_analysis_input(row: sqlite3.Row) -> dict[str, Any]:
    """Rebuild exactly the persisted inputs used by evidence model v2."""
    review = json.loads(row["snapshot_json"])
    available = set(row.keys())
    for key in (
        "back_calculated_at",
        "estimated_posted_date",
        "time_parse_status",
        "time_relative_unit",
    ):
        if key in available:
            review[key] = row[key]
    return review


ANALYSIS_LEVEL_LABELS = {
    "content_similar": "內容高度相似",
    "review": "異常組合，待人工複核",
    "suspicious_high": "高優先複核",
}
INDEPENDENT_SIGNAL_RULES = {
    "reviewer_cohort": "reviewer_network",
    "shared_photo": "media",
}


def _load_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _effective_analysis_label(
    stored_label: str,
    evidence_json: str,
    scope: str,
    coverage: dict[str, Any] | None = None,
) -> str:
    evidence = _normalize_analysis_evidence(
        _load_json(evidence_json, []),
        {"label": stored_label, "scope": scope},
        coverage=coverage,
    )
    return _effective_label_for_evidence(evidence)


def _effective_label_for_evidence(evidence: dict[str, Any]) -> str:
    return "suspected" if evidence.get("level") == "suspicious_high" else "highly_similar"


def _normalize_analysis_evidence(
    value: Any,
    group: dict[str, Any],
    *,
    metrics: dict[str, Any] | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose one stable evidence-v2 view while retaining legacy JSON snapshots."""
    legacy_format = not isinstance(value, dict)
    source = dict(value) if isinstance(value, dict) else {}
    try:
        schema_version = int(source.get("schema_version", source.get("version", 1)))
    except (TypeError, ValueError):
        schema_version = 1
    schema_version = max(1, schema_version)
    unsupported_version = schema_version > 2
    legacy = legacy_format or schema_version < 2

    level = str(source.get("level", "")).strip()
    compatibility_downgraded = False
    if legacy or unsupported_version:
        # No pre-v2 payload can satisfy the v2 independent-signal contract,
        # even if it happens to contain a similarly named level field.
        level = "review" if group.get("label") == "suspected" else "content_similar"
        compatibility_downgraded = True
    elif level not in ANALYSIS_LEVEL_LABELS:
        # Old "suspected" snapshots used content-derived rules and therefore must
        # not silently be presented as high-confidence evidence after deployment.
        level = "review" if group.get("label") == "suspected" else "content_similar"
        compatibility_downgraded = True

    default_summaries = {
        "content_similar": "文字內容相似，僅作為候選；目前沒有足夠的獨立行為佐證。",
        "review": "發現需要人工檢視的異常組合；這不代表受託、付費或虛假評論。",
        "suspicious_high": "多項彼此獨立的異常訊號同時出現，建議優先人工複核。",
    }
    raw_summary = source.get("summary", "")
    if isinstance(raw_summary, list):
        summary = "；".join(str(item).strip().rstrip("。") for item in raw_summary if str(item).strip())
        if summary:
            summary += "。"
    else:
        summary = str(raw_summary).strip()
    summary = summary or default_summaries[level]
    if compatibility_downgraded:
        if unsupported_version:
            reason = "目前程式無法驗證這個較新的證據格式，已安全降級。"
        elif legacy:
            reason = "舊版結論不符合證據模型 v2 合約，已安全降級。"
        else:
            reason = "儲存的證據等級無法識別，已安全降級。"
        summary = f"{default_summaries[level]} {reason}"

    raw_signals: list[Any] = []
    source_signals = source.get("signals", [])
    if isinstance(source_signals, dict):
        for key, signal in source_signals.items():
            if isinstance(signal, dict):
                raw_signals.append({"key": key, **signal})
            else:
                raw_signals.append({"key": key, "value": signal})
    elif isinstance(source_signals, list):
        raw_signals.extend(source_signals)
    if legacy_format and isinstance(value, list):
        raw_signals.extend(value)

    for signal in source.get("supporting", []) if isinstance(source.get("supporting"), list) else []:
        raw_signals.append(_signal_with_state(signal, "supporting"))
    for signal in source.get("limitations", []) if isinstance(source.get("limitations"), list) else []:
        raw_signals.append(_signal_with_state(signal, "limitation"))

    signals = [_normalize_evidence_signal(item) for item in raw_signals]
    signals = [item for item in signals if item["label"] or item["detail"]]
    if compatibility_downgraded:
        # Preserve old/unknown facts for inspection, but never present them as
        # v2 supporting evidence or let them populate the independent count.
        for item in signals:
            item["state"] = "neutral"

    metric_source = source.get("metrics", {})
    metric_values = dict(metric_source) if isinstance(metric_source, dict) else {}
    for key, computed in (metrics or {}).items():
        metric_values[key] = computed
    _copy_metric_aliases(metric_values)

    coverage_source = source.get("coverage", {})
    coverage_values = dict(coverage_source) if isinstance(coverage_source, dict) else {}
    nested_coverage = {
        "profiles": ("profile_known_count", "profile_total"),
        "dates": ("date_known_count", "date_total"),
    }
    for nested_key, (known_key, total_key) in nested_coverage.items():
        nested = coverage_values.get(nested_key)
        if isinstance(nested, dict):
            coverage_values.setdefault(known_key, nested.get("known", 0))
            coverage_values.setdefault(total_key, nested.get("total", 0))
    for key, computed in (coverage or {}).items():
        coverage_values[key] = computed
    coverage_values.setdefault("distinct_profiles", metric_values.get("distinct_profiles", 0))
    coverage_values.setdefault("date_known_count", metric_values.get("date_known_count", 0))
    coverage_values.setdefault("date_precise_count", metric_values.get("date_precise_count", 0))
    coverage_values.setdefault("date_total", metric_values.get("date_total", 0))
    coverage_values.setdefault("profile_known_count", metric_values.get("distinct_profiles", 0))
    coverage_values.setdefault("profile_total", metric_values.get("review_count", 0))
    _copy_coverage_aliases(coverage_values)
    date_total = _as_int(coverage_values.get("date_total"))
    date_known = _as_int(coverage_values.get("date_known_count"))
    profile_total = _as_int(coverage_values.get("profile_total"))
    profile_known = _as_int(coverage_values.get("profile_known_count"))
    coverage_values["date_percent"] = date_known / date_total if date_total else 0.0
    coverage_values["profile_percent"] = (
        profile_known / profile_total if profile_total else 0.0
    )

    # Never trust a stored high-level declaration by itself. A v2 high-priority
    # result must still contain at least two independently eligible signal
    # families and three canonical contributor identities. Time proximity is
    # explicitly contextual until a per-shop baseline model exists.
    scope_value = str(group.get("scope", ""))
    input_total = _as_int(coverage_values.get("profile_total"))
    v2_input_count = _as_int(coverage_values.get("v2_input_count"))
    v2_snapshot_complete = input_total > 0 and v2_input_count == input_total
    snapshot_incompatible = (
        not legacy and not unsupported_version and not v2_snapshot_complete
    )
    for item in signals:
        if (
            item.get("independent_for_level") is True
            and item.get("state") == "supporting"
            and (
                snapshot_incompatible
                or _verified_independent_signal_family(item, scope=scope_value) is None
            )
        ):
            item["state"] = "neutral"
    independent_families = sorted(
        {
            family
            for item in signals
            if (
                family := _verified_independent_signal_family(
                    item, scope=scope_value
                )
            )
            is not None
        }
    )
    distinct_profiles = _as_int(coverage_values.get("distinct_profiles"))
    high_eligible = (
        len(independent_families) >= 2
        and distinct_profiles >= 3
        and v2_snapshot_complete
    )
    level_downgraded = False
    snapshot_downgraded = False
    if snapshot_incompatible:
        level = "content_similar"
        snapshot_downgraded = True
    elif not legacy and level == "suspicious_high" and not high_eligible:
        level = "review" if any(item["state"] == "supporting" for item in signals) else "content_similar"
        level_downgraded = True

    decision_source = source.get("decision", {})
    decision = dict(decision_source) if isinstance(decision_source, dict) else {}
    decision.update(
        independent_signal_families=independent_families,
        independent_signal_count=len(independent_families),
        minimum_profiles_for_high=3,
        minimum_signal_families_for_high=2,
        v2_input_count=v2_input_count,
        input_total=input_total,
        v2_snapshot_complete=v2_snapshot_complete,
        high_eligible=high_eligible,
        level_derived_from_score=False,
        score_not_probability=True,
    )

    raw_breakdown = source.get("score_breakdown", [])
    score_breakdown: list[dict[str, Any]] = []
    if isinstance(raw_breakdown, list):
        for raw_item in raw_breakdown:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            try:
                maximum = max(0.0, min(100.0, float(item.get("max_points", 0))))
                points = max(0.0, min(maximum, float(item.get("points", 0))))
            except (TypeError, ValueError):
                continue
            item["max_points"] = maximum
            item["points"] = points
            score_breakdown.append(item)
    if score_breakdown:
        score = min(100.0, sum(float(item["points"]) for item in score_breakdown))
    else:
        score = source.get("score", source.get("evidence_score"))
        try:
            score = max(0.0, min(100.0, float(score))) if score is not None else None
        except (TypeError, ValueError):
            score = None

    if snapshot_downgraded:
        summary = (
            default_summaries[level]
            + " 此快照沒有完整的 v2 輸入指紋，獨立佐證已停用；請重新執行完整分析。"
        )
    elif level_downgraded:
        summary = (
            default_summaries[level]
            + " 儲存的高優先結論不符合目前 v2 硬條件，已自動降級。"
        )
    if compatibility_downgraded or snapshot_downgraded or level_downgraded:
        # A score assembled by an incompatible or internally inconsistent
        # payload cannot be shown as if the current model had verified it.
        score = None
        score_breakdown = []

    return {
        **source,
        "schema_version": schema_version,
        "version_label": (
            f"未知版本證據 v{schema_version}"
            if unsupported_version
            else ("舊版證據" if legacy else f"證據模型 v{schema_version}")
        ),
        "is_legacy": legacy,
        "unsupported_version": unsupported_version,
        "level": level,
        "level_label": ANALYSIS_LEVEL_LABELS[level],
        "summary": summary,
        "score": score,
        "score_breakdown": score_breakdown,
        "signals": signals,
        "supporting": [item for item in signals if item["state"] == "supporting"],
        "limitations": [item for item in signals if item["state"] == "limitation"],
        "neutral": [item for item in signals if item["state"] == "neutral"],
        "metrics": metric_values,
        "coverage": coverage_values,
        "decision": decision,
    }


def _signal_with_state(value: Any, state: str) -> Any:
    if isinstance(value, dict):
        return {**value, "state": value.get("state", state)}
    return {"detail": str(value), "state": state}


def _verified_independent_signal_family(
    signal: dict[str, Any], *, scope: str
) -> str | None:
    """Fail closed when a persisted strong signal contradicts its own facts."""
    key = str(signal.get("key", ""))
    family = INDEPENDENT_SIGNAL_RULES.get(key)
    if (
        family is None
        or str(signal.get("family", "")) != family
        or signal.get("independent_for_level") is not True
        or signal.get("matched") is not True
        or str(signal.get("status", "")).casefold() != "matched"
        or signal.get("state") != "supporting"
    ):
        return None
    observed = signal.get("observed")
    if not isinstance(observed, dict):
        return None
    if key == "reviewer_cohort":
        if scope != "cross":
            return None
        if (
            _as_int(observed.get("largest_cohort_profile_count")) < 2
            or _as_int(observed.get("largest_cohort_shop_count")) < 2
        ):
            return None
    elif key == "shared_photo":
        if (
            _as_int(observed.get("shared_photo_count")) < 1
            or _as_int(observed.get("largest_distinct_profile_count")) < 2
        ):
            return None
    return family


def _normalize_evidence_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "key": "legacy",
            "label": "舊版規則（不計入 v2 證據）",
            "detail": str(value),
            "state": "neutral",
        }
    raw_state = str(value.get("state", value.get("status", ""))).casefold()
    if raw_state in {
        "limitation", "limited", "insufficient", "missing", "unavailable", "weak",
    }:
        state = "limitation"
    elif raw_state in {"not_matched", "not_applicable", "negative", "no"}:
        state = "neutral"
    elif value.get("supported") is True or value.get("matched") is True or raw_state in {
        "supporting", "supported", "matched", "triggered", "positive", "yes",
    }:
        state = "supporting"
    else:
        detail_hint = str(
            value.get("detail", value.get("summary", value.get("description", "")))
        )
        state = "limitation" if any(
            phrase in detail_hint for phrase in ("資料不足", "無足夠", "缺少", "無法判定")
        ) else "neutral"
    key = str(value.get("key", value.get("code", ""))).strip()
    label = str(value.get("label", value.get("name", value.get("title", key)))).strip()
    detail = str(
        value.get("detail", value.get("summary", value.get("description", "")))
    ).strip()
    observed = value.get("observed", value.get("value"))
    threshold = value.get("threshold")
    if not detail and observed is not None:
        detail = f"觀察值：{observed}"
    return {
        **value,
        "key": key,
        "label": label,
        "detail": detail,
        "state": state,
        "observed": observed,
        "threshold": threshold,
    }


def _copy_metric_aliases(values: dict[str, Any]) -> None:
    aliases = {
        "min_lexical": ("lexical_min",),
        "median_lexical": ("lexical_median",),
        "max_lexical": ("lexical_max",),
        "min_semantic": ("semantic_min",),
        "median_semantic": ("semantic_median",),
        "max_semantic": ("semantic_max",),
        "pair_density": ("density", "edge_density"),
        "direct_pairs": ("edge_count", "pair_count"),
        "possible_pairs": ("comparable_pairs", "total_pairs"),
    }
    for canonical, alternatives in aliases.items():
        if canonical in values:
            continue
        for alternative in alternatives:
            if alternative in values:
                values[canonical] = values[alternative]
                break


def _copy_coverage_aliases(values: dict[str, Any]) -> None:
    aliases = {
        "profile_known_count": ("profiles_known", "known_profiles"),
        "distinct_profiles": ("profile_count",),
        "profile_total": ("profiles_total", "review_count"),
        "date_known_count": ("dates_known", "known_dates"),
        "date_precise_count": ("dates_precise", "precise_dates"),
        "date_total": ("dates_total", "review_count"),
    }
    for canonical, alternatives in aliases.items():
        if canonical in values:
            continue
        for alternative in alternatives:
            if alternative in values:
                values[canonical] = values[alternative]
                break


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
