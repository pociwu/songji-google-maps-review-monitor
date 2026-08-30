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
    assert resolved["two_hour_slot"] is None
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
    assert result["weekday_time_count"] == 0
    assert result["two_hour_period_count"] == 0
    assert result["two_hour_period_crossing_count"] == 0
    assert result["date_estimate_count"] == 1
    assert result["coarse_date_count"] == 1
    assert sum(slot["count"] for slot in result["time_slots"]) == 0
    assert sum(
        cell["count"]
        for row in result["weekday_time_heatmap"]["rows"]
        for cell in row["cells"]
    ) == 0
    assert [item["count"] for item in result["two_hour_periods"]] == [0] * 12


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
    assert len(result["weekday_time_heatmap"]["rows"]) == 12
    assert all(
        len(row["cells"]) == 7
        for row in result["weekday_time_heatmap"]["rows"]
    )


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


def test_weekday_two_hour_heatmap_has_fixed_axes_and_counts():
    monday = review("monday-1", "2026-08-24T10:30:00+08:00", "minutes")
    same_id_other_shop = dict(monday)
    same_id_other_shop["shop_key"] = "shop-b"
    same_id_other_shop["shop_name"] = "乙店"
    items = [
        monday,
        dict(monday),
        same_id_other_shop,
        review("monday-2", "2026-08-24T10:45:00+08:00", "seconds"),
        review("tuesday", "2026-08-25T10:30:00+08:00", "minutes"),
        review("sunday", "2026-08-30T22:30:00+08:00", "minutes"),
    ]

    result = build_review_time_analysis(
        items, "Asia/Taipei", 30, reference_date=date(2026, 8, 30)
    )
    heatmap = result["weekday_time_heatmap"]

    assert result["weekday_time_count"] == 5
    assert heatmap["weekday_labels"] == (
        "週一", "週二", "週三", "週四", "週五", "週六", "週日"
    )
    assert [row["slot_label"] for row in heatmap["rows"]] == [
        "00–01", "02–03", "04–05", "06–07", "08–09", "10–11",
        "12–13", "14–15", "16–17", "18–19", "20–21", "22–23",
    ]
    assert all(len(row["cells"]) == 7 for row in heatmap["rows"])
    assert heatmap["rows"][5]["cells"][0]["count"] == 3
    assert heatmap["rows"][5]["cells"][1]["count"] == 1
    assert heatmap["rows"][11]["cells"][6]["count"] == 1
    assert heatmap["rows"][5]["cells"][0]["level"] == 5
    assert sum(
        cell["count"] for row in heatmap["rows"] for cell in row["cells"]
    ) == heatmap["eligible_count"] == 5


def test_two_hour_heatmap_excludes_uncertainty_crossing_slot_boundary():
    crossing = review("crossing", "2026-08-24T10:30:00+08:00", "hours")
    contained = review("contained", "2026-08-24T11:30:00+08:00", "hours")

    crossing_time = resolve_review_posted_time(crossing, "Asia/Taipei")
    contained_time = resolve_review_posted_time(contained, "Asia/Taipei")
    result = build_review_time_analysis(
        [crossing, contained],
        "Asia/Taipei",
        30,
        reference_date=date(2026, 8, 30),
    )

    assert crossing_time is not None and crossing_time["time_slot"] == 3
    assert crossing_time["two_hour_slot"] is None
    assert contained_time is not None and contained_time["two_hour_slot"] == 5
    assert result["time_slot_count"] == 2
    assert result["weekday_time_count"] == 1
    assert result["weekday_time_heatmap"]["rows"][5]["cells"][0]["count"] == 1
    assert result["two_hour_period_count"] == 1
    assert result["two_hour_period_crossing_count"] == 1
    assert result["two_hour_periods"][5]["count"] == 1


def test_two_hour_distribution_uses_boundaries_and_precision_breakdown():
    items = [
        review("midnight", "2026-08-24T00:30:00+08:00", "minutes"),
        review("two", "2026-08-24T02:00:01+08:00", "seconds"),
        review("two-boundary", "2026-08-24T02:00:00+08:00", "seconds"),
        review("noon", "2026-08-24T12:01:00+08:00", "minutes"),
        review("noon-boundary", "2026-08-24T12:00:00+08:00", "minutes"),
        review("evening", "2026-08-24T19:00:00+08:00", "hours"),
        review("evening-boundary", "2026-08-24T18:30:00+08:00", "hours"),
    ]

    result = build_review_time_analysis(
        items, "Asia/Taipei", 30, reference_date=date(2026, 8, 30)
    )

    assert result["two_hour_period_count"] == result["weekday_time_count"] == 4
    assert result["two_hour_period_crossing_count"] == 3
    assert [item["range_label"] for item in result["two_hour_periods"]] == [
        "00:00–01:59", "02:00–03:59", "04:00–05:59", "06:00–07:59",
        "08:00–09:59", "10:00–11:59", "12:00–13:59", "14:00–15:59",
        "16:00–17:59", "18:00–19:59", "20:00–21:59", "22:00–23:59",
    ]
    expected_counts = [1, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0]
    assert [item["count"] for item in result["two_hour_periods"]] == expected_counts
    assert [item["percent"] for item in result["two_hour_periods"]] == [
        25.0, 25.0, 0.0, 0.0, 0.0, 0.0, 25.0, 0.0, 0.0, 25.0, 0.0, 0.0
    ]
    assert [item["width_percent"] for item in result["two_hour_periods"]] == [
        25.0, 25.0, 0.0, 0.0, 0.0, 0.0, 25.0, 0.0, 0.0, 25.0, 0.0, 0.0
    ]
    assert [item["fine_count"] for item in result["two_hour_periods"]] == [
        1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0
    ]
    assert [item["hour_count"] for item in result["two_hour_periods"]] == [
        0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0
    ]
    assert all(
        item["fine_count"] + item["hour_count"] == item["count"]
        for item in result["two_hour_periods"]
    )
    assert sum(item["count"] for item in result["two_hour_periods"]) == 4


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
