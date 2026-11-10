from datetime import datetime, timedelta
from pathlib import Path
from hashlib import sha256
import sqlite3

from maps_review_monitor.database import Database
from maps_review_monitor.models import OwnerReply, ProfileSummary, ReviewSnapshot


def sample(text="很好", reply=None):
    return ReviewSnapshot(
        review_id="r1", shop_key="s1", shop_name="店家", shop_url="https://google.com/maps/x",
        author="小明", stars=5, time_text="2 小時前", text=text,
        profile=ProfileSummary(url="https://google.com/maps/contrib/1", local_guide=True, review_count=12),
        photo_urls=["https://example.test/a.jpg"], photo_paths=["/tmp/a.jpg"], owner_reply=reply,
    )


def test_hash_excludes_local_paths():
    first = sample()
    second = sample()
    second.photo_paths = ["D:/restored/a.jpg"]
    assert first.content_hash() == second.content_hash()


def test_remote_metadata_changes_do_not_trigger_review_update(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    db.sync_shop("s1", "店家", "https://google.com/maps/x")
    first = sample()
    db.upsert_review(first, notify=False)
    # Simulate a row whose hash was produced by the previous hash scheme.
    db.conn.execute(
        "UPDATE reviews SET content_hash='legacy-hash' WHERE shop_key='s1' AND review_id='r1'"
    )
    db.conn.commit()

    second = sample()
    second.profile.url = "https://google.com/maps/contrib/1/reviews?hl=zh-TW"
    second.photo_urls = ["https://lh3.googleusercontent.com/new-signed-token"]

    assert db.upsert_review(second, notify=True) == []
    stored = db.get_review("s1", "r1")
    assert stored is not None
    assert stored.profile.url == second.profile.url
    assert stored.photo_urls == second.photo_urls
    db.close()


def test_relative_times_and_profile_totals_do_not_trigger_updates():
    first = sample(reply=OwnerReply("謝謝", "1 天前"))
    second = sample(reply=OwnerReply("謝謝", "2 天前"))
    second.time_text = "2 天前"
    second.profile.review_count = 99
    assert first.content_hash() == second.content_hash()
    assert first.reply_hash() == second.reply_hash()


def test_author_rating_and_text_remain_notification_content():
    baseline = sample()
    renamed = sample()
    renamed.author = "小華"
    rerated = sample()
    rerated.stars = 4
    rewritten = sample("真的很好")

    assert baseline.content_hash() != renamed.content_hash()
    assert baseline.content_hash() != rerated.content_hash()
    assert baseline.content_hash() != rewritten.content_hash()


def test_database_detects_new_update_and_reply(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    db.sync_shop("s1", "店家", "https://google.com/maps/x")
    assert db.upsert_review(sample(), notify=False) == []
    assert db.upsert_review(sample("修改後"), notify=True) == ["review_updated"]
    changed = sample("修改後", OwnerReply("謝謝", "剛剛"))
    assert db.upsert_review(changed, notify=True) == ["owner_reply_added"]
    db.close()


def test_database_tracks_back_calculation_and_long_transition(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    db.sync_shop("s1", "店家", "https://google.com/maps/x")

    recent = sample()
    recent.time_text = "17 分鐘前"
    db.upsert_review(recent, notify=False, observed_at="2026-07-19T06:40:35+00:00")
    row = db.conn.execute(
        "SELECT back_calculated_at,time_parse_status FROM reviews WHERE review_id='r1'"
    ).fetchone()
    assert row["back_calculated_at"] == "2026-07-19T14:23:35+08:00"
    assert row["time_parse_status"] == "short"
    stored = next(db.iter_reviews("s1"))
    assert stored["time_relative_unit"] == "minutes"

    older = sample()
    older.review_id = "r2"
    older.time_text = "2 週前"
    db.upsert_review(older, notify=False, observed_at="2026-07-20T02:00:00+00:00")
    db.upsert_review(older, notify=False, observed_at="2026-07-20T03:00:00+00:00")
    older.time_text = "3 週前"
    db.upsert_review(older, notify=False, observed_at="2026-07-20T04:00:00+00:00")
    row = db.conn.execute(
        "SELECT estimated_posted_date,time_parse_status FROM reviews WHERE review_id='r2'"
    ).fetchone()
    assert row["estimated_posted_date"] == "2026-06-29"
    assert row["time_parse_status"] == "long"

    recent.time_text = "1 週前"
    db.upsert_review(recent, notify=False, observed_at="2026-07-26T06:40:35+00:00")
    recent.time_text = "2 週前"
    db.upsert_review(recent, notify=False, observed_at="2026-08-02T06:40:35+00:00")
    row = db.conn.execute(
        "SELECT back_calculated_at,estimated_posted_date FROM reviews WHERE review_id='r1'"
    ).fetchone()
    assert row["back_calculated_at"] == "2026-07-19T14:23:35+08:00"
    assert row["estimated_posted_date"] is None
    db.close()


def test_legacy_database_is_backed_up_before_migration(tmp_path: Path):
    path = tmp_path / "reviews.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE reviews(
             shop_key TEXT NOT NULL,review_id TEXT NOT NULL,snapshot_json TEXT NOT NULL,
             content_hash TEXT NOT NULL,reply_hash TEXT NOT NULL,
             first_seen_at TEXT NOT NULL,updated_at TEXT NOT NULL,
             PRIMARY KEY(shop_key,review_id))"""
    )
    connection.commit()
    connection.close()

    db = Database(path)
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(reviews)")}
    db.close()
    assert {"back_calculated_at", "estimated_posted_date", "time_parse_status"} <= columns
    assert len(list(tmp_path.glob("reviews.sqlite3.bak-*"))) == 1


def test_event_is_idempotent_and_parts_are_independent(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    db.sync_shop("s1", "店家", "https://google.com/maps/x")
    payload = {"shop_key": "s1", "review_id": "r1"}
    parts = [{"kind": "text", "text": "a"}, {"kind": "text", "text": "b"}]
    assert db.add_event("unique", "review_new", payload, parts)
    assert not db.add_event("unique", "review_new", payload, parts)
    due = db.due_parts()
    assert len(due) == 2
    db.mark_part_sent(due[0]["id"])
    assert len(db.due_parts()) == 1
    db.close()


def test_notification_claim_is_atomic_across_two_database_connections(tmp_path: Path):
    path = tmp_path / "db.sqlite3"
    first_worker = Database(path)
    assert first_worker.add_event(
        "claim-once", "review_new", {}, [{"kind": "text", "text": "a"}]
    )
    second_worker = Database(path)

    first_claim = first_worker.claim_due_parts(limit=1)
    second_claim = second_worker.claim_due_parts(limit=1)

    assert len(first_claim) == 1
    assert first_claim[0]["status"] == "sending"
    assert first_claim[0]["claim_token"]
    assert second_claim == []
    first_worker.close()
    second_worker.close()


def test_expired_claim_lease_is_recovered_without_stale_worker_overwrite(tmp_path: Path):
    path = tmp_path / "db.sqlite3"
    first_worker = Database(path)
    assert first_worker.add_event(
        "recover-claim", "review_new", {}, [{"kind": "text", "text": "a"}]
    )
    second_worker = Database(path)
    first_claim = first_worker.claim_due_parts(limit=1, lease_seconds=60)[0]
    old_token = first_claim["claim_token"]
    first_worker.conn.execute(
        """UPDATE notification_parts
           SET claim_expires_at='2000-01-01T00:00:00+00:00'
           WHERE id=?""",
        (first_claim["id"],),
    )
    first_worker.conn.commit()

    recovered = second_worker.claim_due_parts(limit=1, lease_seconds=60)[0]
    assert recovered["id"] == first_claim["id"]
    assert recovered["claim_token"] != old_token
    assert first_worker.mark_part_failed(
        first_claim["id"], 1, "late worker", old_token
    ) is False
    current = second_worker.conn.execute(
        "SELECT status,claim_token,attempts FROM notification_parts WHERE id=?",
        (recovered["id"],),
    ).fetchone()
    assert dict(current) == {
        "status": "sending", "claim_token": recovered["claim_token"], "attempts": 0,
    }
    first_worker.close()
    second_worker.close()


def test_new_notification_parts_expire_seven_days_after_event(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    payload = {"shop_key": "s1", "review_id": "r1"}
    assert db.add_event(
        "ttl", "review_new", payload, [{"kind": "text", "text": "a"}]
    )
    row = db.conn.execute(
        """SELECT e.created_at,np.expires_at
           FROM events e JOIN notification_parts np ON np.event_id=e.id
           WHERE e.event_key='ttl'"""
    ).fetchone()
    created_at = datetime.fromisoformat(row["created_at"])
    expires_at = datetime.fromisoformat(row["expires_at"])
    assert expires_at - created_at == timedelta(days=7)
    db.close()


def test_due_parts_terminalizes_expired_and_exhausted_parts(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    payload = {"shop_key": "s1", "review_id": "r1"}
    assert db.add_event(
        "terminal", "review_new", payload,
        [{"kind": "text", "text": "expired"}, {"kind": "text", "text": "dead"}],
    )
    rows = db.conn.execute(
        "SELECT id,part_no FROM notification_parts ORDER BY part_no"
    ).fetchall()
    db.conn.execute(
        "UPDATE notification_parts SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (rows[0]["id"],),
    )
    db.conn.execute(
        "UPDATE notification_parts SET attempts=8 WHERE id=?", (rows[1]["id"],)
    )
    db.conn.commit()

    assert db.due_parts() == []
    statuses = dict(
        db.conn.execute("SELECT part_no,status FROM notification_parts ORDER BY part_no")
    )
    assert statuses == {0: "expired", 1: "dead"}
    db.close()


def test_eighth_notification_failure_is_immediately_dead(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    assert db.add_event(
        "failure-limit", "review_new", {}, [{"kind": "text", "text": "a"}]
    )
    part_id = db.due_parts()[0]["id"]
    db.mark_part_failed(part_id, 8, "still failing")
    row = db.conn.execute(
        "SELECT status,attempts,last_error FROM notification_parts WHERE id=?", (part_id,)
    ).fetchone()
    assert dict(row) == {
        "status": "dead", "attempts": 8, "last_error": "still failing"
    }
    assert db.due_parts() == []
    db.close()


def test_notification_expiry_migration_is_backed_up_and_backfilled(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version=3;
        CREATE TABLE events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL, shop_key TEXT, review_id TEXT,
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE notification_parts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
          part_no INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT NOT NULL, sent_at TEXT, last_error TEXT,
          UNIQUE(event_id, part_no)
        );
        INSERT INTO events(
          event_key,event_type,payload_json,created_at
        ) VALUES('legacy','review_new','{}','2000-01-01T00:00:00+00:00');
        INSERT INTO notification_parts(
          event_id,part_no,kind,payload_json,next_attempt_at
        ) VALUES(1,0,'text','{}','2000-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    db = Database(path)
    row = db.conn.execute(
        "SELECT expires_at FROM notification_parts WHERE id=1"
    ).fetchone()
    assert row["expires_at"] == "2000-01-08T00:00:00+00:00"
    assert db.due_parts() == []
    assert db.conn.execute(
        "SELECT status FROM notification_parts WHERE id=1"
    ).fetchone()["status"] == "expired"
    columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(notification_parts)")
    }
    assert {"expires_at", "claim_token", "claimed_at", "claim_expires_at"} <= columns
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    db.close()
    backups = list(tmp_path.glob("legacy.sqlite3.bak-*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        columns = {row[1] for row in backup.execute("PRAGMA table_info(notification_parts)")}
        assert "expires_at" not in columns
    finally:
        backup.close()


def test_notification_migration_terminalizes_orphan_once(tmp_path: Path):
    path = tmp_path / "orphan.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version=3;
        CREATE TABLE events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL, shop_key TEXT, review_id TEXT,
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE notification_parts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
          part_no INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT NOT NULL, sent_at TEXT, last_error TEXT,
          UNIQUE(event_id, part_no)
        );
        INSERT INTO notification_parts(
          event_id,part_no,kind,payload_json,next_attempt_at
        ) VALUES(999,0,'text','{}','2000-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    first = Database(path)
    row = first.conn.execute(
        """SELECT status,expires_at,claim_token,claimed_at,claim_expires_at
           FROM notification_parts"""
    ).fetchone()
    assert dict(row) == {
        "status": "expired",
        "expires_at": "1970-01-08T00:00:00+00:00",
        "claim_token": None,
        "claimed_at": None,
        "claim_expires_at": None,
    }
    first.close()
    assert len(list(tmp_path.glob("orphan.sqlite3.bak-*"))) == 1

    second = Database(path)
    second.close()
    assert len(list(tmp_path.glob("orphan.sqlite3.bak-*"))) == 1


def test_legacy_analysis_evidence_is_read_as_review_level_v2_view(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    db.sync_shop("s1", "店家", "https://google.com/maps/x")
    first = sample("第一則相似內容")
    second = sample("第二則相似內容")
    second.review_id = "r2"
    second.author = "小華"
    second.profile.url = "https://google.com/maps/contrib/2"
    db.upsert_review(first, notify=False)
    db.upsert_review(second, notify=False)
    snapshots = (first, second)
    run_id = db.start_analysis("legacy-model", {}, 2)
    db.complete_analysis(
        run_id,
        [
            {
                "shop_key": item.shop_key,
                "review_id": item.review_id,
                "text_hash": sha256(item.text.encode("utf-8")).hexdigest(),
                "label": "suspected",
                "direction": "positive",
                "max_lexical": 0.9,
                "max_semantic": 0.95,
            }
            for item in snapshots
        ],
        [
            {
                "group_id": "same-legacy",
                "scope": "same",
                "fingerprint": "legacy",
                "label": "suspected",
                "direction": "positive",
                "review_count": 2,
                "shop_count": 1,
                "max_lexical": 0.9,
                "max_semantic": 0.95,
                "evidence": ["舊版固定門檻規則"],
                "members": [
                    {
                        "shop_key": item.shop_key,
                        "review_id": item.review_id,
                        "max_lexical": 0.9,
                        "max_semantic": 0.95,
                    }
                    for item in snapshots
                ],
            }
        ],
        [
            {
                "scope": "same",
                "left_shop_key": "s1",
                "left_review_id": "r1",
                "right_shop_key": "s1",
                "right_review_id": "r2",
                "lexical": 0.9,
                "semantic": 0.95,
            }
        ],
    )

    listed_group = db.analysis_groups(scope="same")[0]
    listed = listed_group["evidence"]
    assert listed_group["label"] == "highly_similar"
    assert listed["schema_version"] == 1
    assert listed["is_legacy"] is True
    assert listed["level"] == "review"
    assert listed["level_label"] == "異常組合，待人工複核"
    assert listed["supporting"] == []
    assert listed["neutral"][0]["detail"] == "舊版固定門檻規則"
    assert db.analysis_shop_counts()["s1"] == {"similar": 2, "suspected": 0}
    assert {item["label"] for item in db.review_analysis("s1").values()} == {
        "highly_similar"
    }
    detailed, _ = db.analysis_group("same-legacy")
    assert detailed["evidence"]["metrics"]["pair_density"] == 1
    assert detailed["evidence"]["coverage"]["distinct_profiles"] == 2

    db.conn.execute(
        "UPDATE analysis_groups SET evidence_json=? WHERE group_id='same-legacy'",
        ('{"schema_version":1,"level":"suspicious_high"}',),
    )
    db.conn.commit()
    legacy_dict_group = db.analysis_groups(scope="same")[0]
    assert legacy_dict_group["label"] == "highly_similar"
    assert legacy_dict_group["evidence"]["level"] == "review"
    assert legacy_dict_group["evidence"]["is_legacy"] is True
    db.close()
