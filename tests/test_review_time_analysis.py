from datetime import date, timedelta

from maps_review_monitor.review_time_analysis import (
    build_review_time_analysis,
    resolve_review_posted_time,
)


def review(
    review_id: str,
    posted_at: str | None = None,
    unit: str | None = "hours",
    estimated_date: str | None = None,
) -> dict:
    return {
        "shop_key": "shop-a",
        "shop_name": "甲店",
        "review_id": review_id,
        "author": f"作者 {review_id}",
        "stars": 5,
        "time_text": "測試相對時間",
        "first_seen_at": "2026-08-30T12:00:00+08:00",
        "back_calculated_at": posted_at,
        "estimated_posted_date": estimated_date,
        "time_relative_unit": unit,
    }


def test_resolver_uses_taipei_timezone_and_rejects_boundary_for_hour_chart():
    item = review("1", "2026-08-10T16:30:00+00:00", "hours")

    resolved = resolve_review_posted_time(item, "Asia/Taipei")

    assert resolved is not None
    assert resolved["display"] == "2026-08-11 00:30"
    assert resolved["precision"] == "time"
    assert resolved["date_certain"] is False
    assert resolved["time_slot"] is None
    assert resolved["precision_label"] == "有時分回推"


def test_days_and_long_estimates_never_enter_hour_buckets():
    items = [
        review("days", "2026-08-20T10:30:00+08:00", "days"),
        review("months", None, "months", "2026-08-10"),
    ]

    result = build_review_time_analysis(
        items, "Asia/Taipei", 30, reference_date=date(2026, 8, 30)
    )

    assert result["period_count"] == 2
    assert result["exact_time_count"] == 0
    assert result["time_slot_count"] == 0
    assert result["date_estimate_count"] == 1
    assert result["coarse_date_count"] == 1
    assert sum(slot["count"] for slot in result["time_slots"]) == 0


def test_first_seen_time_is_never_used_as_posted_time():
    result = build_review_time_analysis(
        [review("unknown", None, None, None)],
        "Asia/Taipei",
        30,
        reference_date=date(2026, 8, 30),
    )

    assert result["known_count"] == 0
    assert result["unknown_count"] == 1
    assert result["period_count"] == 0


def test_regular_seven_day_sequence_is_reported_with_evidence():
    start = date(2026, 6, 1)
    items = [
        review(
            str(index),
            f"{(start + timedelta(days=index * 7)).isoformat()}T10:30:00+08:00",
            "hours",
        )
        for index in range(8)
    ]

    result = build_review_time_analysis(
        items, "Asia/Taipei", 90, reference_date=date(2026, 7, 31)
    )

    assert result["verdict"]["status"] == "possible"
    interval = next(item for item in result["findings"] if item["kind"] == "interval")
    assert "每 7 天" in interval["title"]
    assert "8 個發文日" in interval["evidence"]


def test_fixed_weekday_and_time_slot_require_precise_cross_period_samples():
    start = date(2026, 5, 4)
    items = [
        review(
            str(index),
            f"{(start + timedelta(days=index * 7)).isoformat()}T10:30:00+08:00",
            "hours",
        )
        for index in range(12)
    ]

    result = build_review_time_analysis(
        items, "Asia/Taipei", 180, reference_date=date(2026, 8, 1)
    )

    kinds = {item["kind"] for item in result["findings"]}
    assert {"weekday", "time-slot", "interval"} <= kinds


def test_small_or_short_lived_sample_is_not_called_regular():
    items = [
        review(str(index), f"2026-08-{20 + index:02d}T10:30:00+08:00", "hours")
        for index in range(5)
    ]

    result = build_review_time_analysis(
        items, "Asia/Taipei", 30, reference_date=date(2026, 8, 30)
    )

    assert result["verdict"]["status"] == "insufficient"
    assert result["findings"] == []


def test_trend_fills_empty_days_and_deduplicates_review_ids():
    duplicate = review("same", "2026-08-30T10:30:00+08:00", "hours")
    result = build_review_time_analysis(
        [duplicate, dict(duplicate)],
        "Asia/Taipei",
        30,
        reference_date=date(2026, 8, 30),
    )

    assert result["total_count"] == 1
    assert len(result["trend"]) == 30
    assert sum(item["count"] for item in result["trend"]) == 1


def test_days_data_is_excluded_from_weekday_chart_and_burst_detection():
    start = date(2026, 6, 1)
    items = []
    for week in range(9):
        weekly_count = 10 if week == 4 else 2
        posted_date = start + timedelta(days=week * 7)
        for index in range(weekly_count):
            items.append(
                review(
                    f"{week}-{index}",
                    f"{posted_date.isoformat()}T10:30:00+08:00",
                    "days",
                )
            )

    result = build_review_time_analysis(
        items, "Asia/Taipei", 90, reference_date=date(2026, 8, 30)
    )

    assert sum(item["count"] for item in result["weekdays"]) == 0
    assert all(item["kind"] != "burst" for item in result["findings"])
