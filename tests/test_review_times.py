from maps_review_monitor.review_times import (
    RelativeTime,
    back_calculate,
    estimate_from_transition,
    parse_relative_time,
)


def test_parse_relative_time_locales():
    assert parse_relative_time("17 分鐘前") == RelativeTime(17, "minutes")
    assert parse_relative_time("2 週前") == RelativeTime(2, "weeks")
    assert parse_relative_time("3 個月前") == RelativeTime(3, "months")
    assert parse_relative_time("1年前") == RelativeTime(1, "years")
    assert parse_relative_time("17分前") == RelativeTime(17, "minutes")
    assert parse_relative_time("2 weeks ago") == RelativeTime(2, "weeks")
    assert parse_relative_time("昨天") == RelativeTime(1, "days")
    assert parse_relative_time("無日期") is None


def test_back_calculate_uses_taipei_time():
    result = back_calculate(
        RelativeTime(17, "minutes"),
        "2026-07-19T06:40:35+00:00",
        "Asia/Taipei",
    )
    assert result == "2026-07-19T14:23:35+08:00"


def test_week_transition_estimates_date_from_midpoint():
    result = estimate_from_transition(
        "2026-07-20T02:00:00+00:00",
        "2026-07-20T04:00:00+00:00",
        RelativeTime(3, "weeks"),
        "Asia/Taipei",
    )
    assert result == "2026-06-29"


def test_month_transition_uses_calendar_month():
    result = estimate_from_transition(
        "2026-03-31T01:00:00+00:00",
        "2026-03-31T03:00:00+00:00",
        RelativeTime(1, "months"),
        "Asia/Taipei",
    )
    assert result == "2026-02-28"
