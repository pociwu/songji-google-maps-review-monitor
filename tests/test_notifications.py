from maps_review_monitor.models import OwnerReply, ProfileSummary, ReviewSnapshot
from maps_review_monitor.notifications import build_parts, render_review, split_text


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
