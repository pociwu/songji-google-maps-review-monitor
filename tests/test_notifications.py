from maps_review_monitor.database import Database
from maps_review_monitor.monitor import enqueue_review, should_enqueue_review
from maps_review_monitor.models import OwnerReply, ProfileSummary, ReviewSnapshot
from maps_review_monitor.notifications import (
    build_parts,
    deliver_due,
    render_review,
    split_text,
)


def review():
    return ReviewSnapshot(
        review_id="r", shop_key="s", shop_name="咖啡店", shop_url="https://google.com/maps/x",
        author="王小美", stars=4, time_text="1 天前", text="好喝",
        profile=ProfileSummary(url="https://google.com/maps/contrib/x", local_guide=True, review_count=8, photo_count=3),
        photo_paths=[f"/tmp/{i}.jpg" for i in range(12)], owner_reply=OwnerReply("謝謝", "剛剛"),
    )


def test_long_text_is_not_lost():
    value = "a" * 9000
    parts = split_text(value, 1000)
    reconstructed = "".join(part.split("\n", 1)[1] for part in parts)
    assert reconstructed == value
    assert len(parts) == 9


def test_review_render_and_media_batches():
    item = review()
    item.back_calculated_at = "2026-07-19T14:23:35+08:00"
    text = render_review("review_new", item, "Asia/Taipei")
    assert "新評論" in text and "王小美" in text and "店家回覆" in text
    assert "回推發文時間：2026-07-19 14:23:35 CST" in text
    parts = build_parts("review_new", review(), "Asia/Taipei")
    media = [p for p in parts if p["kind"] == "media"]
    assert [len(p["paths"]) for p in media] == [10, 2]


def test_old_discovered_reviews_are_not_treated_as_new_notifications():
    assert should_enqueue_review("review_new", "71 小時前") is True
    assert should_enqueue_review("review_new", "2 天前") is True
    assert should_enqueue_review("review_new", "72 小時前") is False
    assert should_enqueue_review("review_new", "3 天前") is False
    assert should_enqueue_review("review_new", "2 週前") is False
    assert should_enqueue_review("review_new", "3 個月前") is False
    assert should_enqueue_review("review_new", "1 年前") is False


def test_unknown_dates_and_non_new_events_are_not_silently_filtered():
    assert should_enqueue_review("review_new", "Google 剛更新日期格式") is True
    assert should_enqueue_review("review_updated", "2 個月前") is True
    assert should_enqueue_review("owner_reply_added", "2 個月前") is True
    assert should_enqueue_review("owner_reply_removed", "2 個月前") is True


def test_enqueue_review_stores_no_event_for_clearly_old_new_review(tmp_path):
    db = Database(tmp_path / "reviews.sqlite3")
    item = review()
    item.time_text = "2 個月前"
    db.sync_shop(item.shop_key, item.shop_name, item.shop_url)
    db.upsert_review(item, notify=False)

    assert enqueue_review(db, "review_new", item, "Asia/Taipei", tmp_path) is False
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert enqueue_review(db, "review_updated", item, "Asia/Taipei", tmp_path) is True
    assert db.conn.execute("SELECT event_type FROM events").fetchone()[0] == "review_updated"
    db.close()


def test_delivery_rechecks_expiry_after_inter_message_wait(tmp_path, monkeypatch):
    db = Database(tmp_path / "reviews.sqlite3")
    assert db.add_event(
        "expiry-during-batch",
        "review_new",
        {},
        [{"kind": "text", "text": "first"}, {"kind": "text", "text": "second"}],
    )

    class RecordingClient:
        def __init__(self):
            self.sent = []

        def send_part(self, part):
            self.sent.append(part["text"])

    client = RecordingClient()

    def expire_claimed_part(_seconds):
        db.conn.execute(
            """UPDATE notification_parts
               SET expires_at='2000-01-01T00:00:00+00:00'
               WHERE status='sending'"""
        )
        db.conn.commit()

    monkeypatch.setattr(
        "maps_review_monitor.notifications.time.sleep", expire_claimed_part
    )
    sent, failed = deliver_due(
        db, client, limit=2, delay_min_seconds=0, delay_max_seconds=0
    )

    assert (sent, failed) == (1, 0)
    assert client.sent == ["first"]
    statuses = dict(
        db.conn.execute("SELECT part_no,status FROM notification_parts ORDER BY part_no")
    )
    assert statuses == {0: "sent", 1: "expired"}
    db.close()


def test_failed_claim_returns_to_pending_without_claim_metadata(tmp_path):
    db = Database(tmp_path / "reviews.sqlite3")
    assert db.add_event(
        "claimed-failure", "review_new", {}, [{"kind": "text", "text": "a"}]
    )

    class FailingClient:
        def send_part(self, _part):
            raise RuntimeError("network down")

    assert deliver_due(
        db, FailingClient(), limit=1, delay_min_seconds=0, delay_max_seconds=0
    ) == (0, 1)
    row = db.conn.execute(
        """SELECT status,attempts,claim_token,claimed_at,claim_expires_at
           FROM notification_parts"""
    ).fetchone()
    assert dict(row) == {
        "status": "pending",
        "attempts": 1,
        "claim_token": None,
        "claimed_at": None,
        "claim_expires_at": None,
    }
    db.close()
