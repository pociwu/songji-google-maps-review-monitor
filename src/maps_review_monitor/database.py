from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .models import ReviewSnapshot
from .review_times import back_calculate, estimate_from_transition, parse_relative_time


SCHEMA_VERSION = 3


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
        rows = self.conn.execute(
            """SELECT agr.shop_key,
                      COUNT(DISTINCT CASE WHEN ag.label='highly_similar' THEN agr.review_id END) similar,
                      COUNT(DISTINCT CASE WHEN ag.label='suspected' THEN agr.review_id END) suspected
               FROM analysis_group_reviews agr
               JOIN analysis_groups ag ON ag.run_id=agr.run_id AND ag.group_id=agr.group_id
               WHERE agr.run_id=? AND ag.excluded_reason IS NULL
               GROUP BY agr.shop_key""",
            (run_id,),
        )
        for row in rows:
            # Recount below after excluding groups made stale by an edited review.
            result[row["shop_key"]] = {"similar": 0, "suspected": 0}
        counted: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in self.conn.execute(
            """SELECT ag.group_id,ag.label,agr.shop_key,agr.review_id
               FROM analysis_groups ag JOIN analysis_group_reviews agr
                 ON agr.run_id=ag.run_id AND agr.group_id=ag.group_id
               WHERE ag.run_id=? AND ag.excluded_reason IS NULL""",
            (run_id,),
        ):
            if row["group_id"] in valid_groups:
                counted[(row["shop_key"], row["label"])].add(row["review_id"])
        for (shop_key, label), review_ids in counted.items():
            result.setdefault(shop_key, {"similar": 0, "suspected": 0})
            result[shop_key]["suspected" if label == "suspected" else "similar"] += len(review_ids)
        return result

    def review_analysis(self, shop_key: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        run_id = self.current_analysis_run_id()
        if run_id is None:
            return {}
        sql = """SELECT ar.*,r.snapshot_json FROM analysis_reviews ar
                 JOIN reviews r ON r.shop_key=ar.shop_key AND r.review_id=ar.review_id
                 WHERE ar.run_id=?"""
        params: list[Any] = [run_id]
        if shop_key:
            sql += " AND ar.shop_key=?"
            params.append(shop_key)
        result = {}
        for row in self.conn.execute(sql, params):
            current_text = str(json.loads(row["snapshot_json"]).get("text", ""))
            if row["text_hash"] != sha256(current_text.encode("utf-8")).hexdigest():
                continue
            item = dict(row)
            item.pop("snapshot_json", None)
            result[(row["shop_key"], row["review_id"])] = item
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
            item["evidence"] = json.loads(item.pop("evidence_json"))
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
        group["evidence"] = json.loads(group.pop("evidence_json"))
        members = []
        for member in self.conn.execute(
            """SELECT agr.max_lexical,agr.max_semantic,r.snapshot_json,
                      r.first_seen_at,r.estimated_posted_date
               FROM analysis_group_reviews agr
               JOIN reviews r ON r.shop_key=agr.shop_key AND r.review_id=agr.review_id
               WHERE agr.run_id=? AND agr.group_id=? ORDER BY r.first_seen_at DESC""",
            (run_id, group_id),
        ):
            item = json.loads(member["snapshot_json"])
            item.update(
                max_lexical=member["max_lexical"], max_semantic=member["max_semantic"],
                first_seen_at=member["first_seen_at"],
                estimated_posted_date=member["estimated_posted_date"],
            )
            members.append(item)
        return group, members

    def _valid_analysis_group_ids(self, run_id: int) -> set[str]:
        """Current pages never count a group whose analyzed reviewer text has since changed."""
        stale: set[str] = set()
        all_groups: set[str] = set()
        rows = self.conn.execute(
            """SELECT agr.group_id,ar.text_hash,r.snapshot_json
               FROM analysis_group_reviews agr
               JOIN analysis_reviews ar
                 ON ar.run_id=agr.run_id AND ar.shop_key=agr.shop_key
                AND ar.review_id=agr.review_id
               JOIN reviews r ON r.shop_key=agr.shop_key AND r.review_id=agr.review_id
               WHERE agr.run_id=?""",
            (run_id,),
        )
        for row in rows:
            all_groups.add(row["group_id"])
            text = str(json.loads(row["snapshot_json"]).get("text", ""))
            if row["text_hash"] != sha256(text.encode("utf-8")).hexdigest():
                stale.add(row["group_id"])
        return all_groups - stale


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
