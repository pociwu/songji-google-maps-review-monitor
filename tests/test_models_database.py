from pathlib import Path
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
