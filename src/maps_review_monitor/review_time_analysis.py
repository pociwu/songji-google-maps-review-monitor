from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from math import comb
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo


WEEKDAY_LABELS = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")
PRECISE_UNITS = {"seconds", "minutes", "hours"}
TIME_SLOT_LABELS = (
    "00–02", "03–05", "06–08", "09–11",
    "12–14", "15–17", "18–20", "21–23",
)
TWO_HOUR_SLOT_LABELS = (
    "00–01", "02–03", "04–05", "06–07", "08–09", "10–11",
    "12–13", "14–15", "16–17", "18–19", "20–21", "22–23",
)
DAYPARTS = (
    ("凌晨", "00:00–05:59"),
    ("上午", "06:00–11:59"),
    ("下午", "12:00–17:59"),
    ("晚上", "18:00–23:59"),
)


def _certain_time_slot(
    local: datetime, earliest: datetime | None, hours_per_slot: int
) -> int | None:
    if earliest is None or earliest.date() != local.date():
        return None
    earliest_slot = earliest.hour // hours_per_slot
    latest_slot = local.hour // hours_per_slot
    return latest_slot if earliest_slot == latest_slot else None


def resolve_review_posted_time(
    review: dict[str, Any], timezone_name: str
) -> dict[str, Any] | None:
    """Resolve the best available posted time without using first_seen_at."""
    timezone = ZoneInfo(timezone_name)
    back_calculated = str(review.get("back_calculated_at") or "").strip()
    relative_unit = str(review.get("time_relative_unit") or "").strip()
    if back_calculated:
        try:
            parsed = datetime.fromisoformat(back_calculated)
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            local = parsed.astimezone(timezone)
        except (TypeError, ValueError):
            local = None
        if local is not None:
            precise = relative_unit in PRECISE_UNITS
            precision = "time" if precise else "date" if relative_unit == "days" else "coarse"
            uncertainty = {
                "seconds": timedelta(seconds=1),
                "minutes": timedelta(minutes=1),
                "hours": timedelta(hours=1),
            }.get(relative_unit)
            earliest = local - uncertainty if uncertainty else None
            date_certain = bool(earliest and earliest.date() == local.date())
            time_slot = _certain_time_slot(local, earliest, 3)
            two_hour_slot = _certain_time_slot(local, earliest, 2)
            daypart_slot = _certain_time_slot(local, earliest, 6)
            return {
                "date": local.date(),
                "datetime": local if precise else None,
                "sort_at": local,
                "precision": precision,
                "date_certain": date_certain,
                "time_slot": time_slot,
                "two_hour_slot": two_hour_slot,
                "daypart_slot": daypart_slot,
                "display": local.strftime("%Y-%m-%d %H:%M") if precise else local.strftime("%Y-%m-%d"),
                "precision_label": (
                    "有時分回推" if precise else "日期級推估" if precision == "date" else "粗略日期"
                ),
                "source_label": (
                    "Google 相對時間回推"
                    if precise
                    else "依「天前」回推日期"
                    if precision == "date"
                    else "舊資料日期回推（來源單位不明）"
                ),
            }

    estimated = str(review.get("estimated_posted_date") or "").strip()
    if estimated:
        try:
            posted_date = date.fromisoformat(estimated)
        except ValueError:
            return None
        return {
            "date": posted_date,
            "datetime": None,
            "sort_at": datetime.combine(posted_date, datetime.min.time(), tzinfo=timezone),
            "precision": "coarse",
            "date_certain": False,
            "time_slot": None,
            "two_hour_slot": None,
            "daypart_slot": None,
            "display": posted_date.isoformat(),
            "precision_label": "粗略日期",
            "source_label": "依 Google 相對時間變化推估",
        }
    return None


def build_review_time_analysis(
    reviews: Iterable[dict[str, Any]],
    timezone_name: str,
    days: int = 90,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Aggregate posted-time evidence and expose transparent pattern signals."""
    if days not in {30, 90, 180, 365}:
        raise ValueError("days must be one of 30, 90, 180, 365")
    timezone = ZoneInfo(timezone_name)
    end_date = reference_date or datetime.now(timezone).date()
    start_date = end_date - timedelta(days=days - 1)
    source_reviews = _deduplicate_reviews(reviews)
    resolved_all: list[dict[str, Any]] = []

    for review in source_reviews:
        resolved = resolve_review_posted_time(review, timezone_name)
        if resolved is None:
            continue
        record = {
            **resolved,
            "shop_key": str(review.get("shop_key") or ""),
            "shop_name": str(review.get("shop_name") or "未知店家"),
            "review_id": str(review.get("review_id") or ""),
            "author": str(review.get("author") or "匿名評論者"),
            "stars": review.get("stars"),
            "time_text": str(review.get("time_text") or ""),
            "time_unit": str(review.get("time_relative_unit") or ""),
        }
        resolved_all.append(record)

    period_records = [
        item for item in resolved_all if start_date <= item["date"] <= end_date
    ]
    period_records.sort(key=lambda item: item["sort_at"], reverse=True)
    exact_records = [item for item in period_records if item["precision"] == "time"]
    date_records = [item for item in period_records if item["precision"] == "date"]
    coarse_records = [item for item in period_records if item["precision"] == "coarse"]
    weekday_records = [item for item in exact_records if item["date_certain"]]
    time_slot_records = [item for item in exact_records if item["time_slot"] is not None]
    weekday_time_records = [
        item for item in exact_records if item["two_hour_slot"] is not None
    ]
    daypart_records = [
        item for item in exact_records if item["daypart_slot"] is not None
    ]

    trend, trend_granularity = _trend_buckets(period_records, start_date, end_date, days)
    weekdays = _distribution(
        WEEKDAY_LABELS,
        Counter(item["date"].weekday() for item in weekday_records),
        len(weekday_records),
    )
    time_slots = _distribution(
        TIME_SLOT_LABELS,
        Counter(item["time_slot"] for item in time_slot_records),
        len(time_slot_records),
    )
    weekday_time_heatmap = _weekday_time_heatmap(weekday_time_records)
    dayparts = _daypart_distribution(daypart_records)
    interval_records = weekday_records + date_records
    burst_records = weekday_records
    findings = _detect_patterns(
        weekday_records,
        time_slot_records,
        interval_records,
        burst_records,
    )
    span_days = (
        (
            max(item["date"] for item in interval_records)
            - min(item["date"] for item in interval_records)
        ).days
        if interval_records else 0
    )
    if len(interval_records) < 8 or span_days < 28:
        verdict = {
            "status": "insufficient",
            "title": "資料不足，尚無法判定規律",
            "detail": f"目前有 {len(interval_records)} 筆可用於規律判讀的回推資料，且涵蓋 {span_days + 1 if interval_records else 0} 天；至少需要 8 筆並跨 28 天。",
        }
    elif findings:
        verdict = {
            "status": "possible",
            "title": "發現可能的時間規律",
            "detail": f"共出現 {len(findings)} 項時間集中或固定間隔線索，仍需搭配評論內容與個人檔案人工確認。",
        }
    else:
        verdict = {
            "status": "none",
            "title": "目前未發現明顯時間規律",
            "detail": "在固定星期、固定時段、固定間隔與短期集中規則下都未達提示門檻。",
        }

    total_count = len(source_reviews)
    known_count = len(resolved_all)
    coverage_percent = round(known_count / total_count * 100) if total_count else 0
    return {
        "days": days,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "total_count": total_count,
        "known_count": known_count,
        "unknown_count": total_count - known_count,
        "coverage_percent": coverage_percent,
        "period_count": len(period_records),
        "exact_time_count": len(exact_records),
        "weekday_eligible_count": len(weekday_records),
        "time_slot_count": len(time_slot_records),
        "weekday_time_count": len(weekday_time_records),
        "daypart_count": len(daypart_records),
        "daypart_crossing_count": len(exact_records) - len(daypart_records),
        "date_estimate_count": len(date_records),
        "coarse_date_count": len(coarse_records),
        "pattern_eligible_count": len(interval_records),
        "trend": trend,
        "trend_granularity": trend_granularity,
        "trend_title": "每日評論發文量" if trend_granularity == "day" else "每週評論發文量",
        "weekdays": weekdays,
        "time_slots": time_slots,
        "weekday_time_heatmap": weekday_time_heatmap,
        "dayparts": dayparts,
        "findings": findings,
        "verdict": verdict,
        "records": period_records[:200],
        "records_truncated": len(period_records) > 200,
    }


def _deduplicate_reviews(reviews: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for review in reviews:
        review_id = str(review.get("review_id") or "")
        key = (str(review.get("shop_key") or ""), review_id)
        if review_id and key in seen:
            continue
        if review_id:
            seen.add(key)
        result.append(review)
    return result


def _trend_buckets(
    records: list[dict[str, Any]], start_date: date, end_date: date, days: int
) -> tuple[list[dict[str, Any]], str]:
    counts = Counter(item["date"] for item in records)
    precise_counts = Counter(item["date"] for item in records if item["precision"] == "time")
    date_counts = Counter(item["date"] for item in records if item["precision"] == "date")
    coarse_counts = Counter(item["date"] for item in records if item["precision"] == "coarse")
    if days == 30:
        values = []
        cursor = start_date
        while cursor <= end_date:
            values.append({"start": cursor, "label": cursor.strftime("%m/%d"), "count": counts[cursor]})
            cursor += timedelta(days=1)
        granularity = "day"
    else:
        week_counts: Counter[date] = Counter()
        week_precise: Counter[date] = Counter()
        week_date: Counter[date] = Counter()
        week_coarse: Counter[date] = Counter()
        for posted_date, count in counts.items():
            week_counts[posted_date - timedelta(days=posted_date.weekday())] += count
        for posted_date, count in precise_counts.items():
            week_precise[posted_date - timedelta(days=posted_date.weekday())] += count
        for posted_date, count in date_counts.items():
            week_date[posted_date - timedelta(days=posted_date.weekday())] += count
        for posted_date, count in coarse_counts.items():
            week_coarse[posted_date - timedelta(days=posted_date.weekday())] += count
        cursor = start_date - timedelta(days=start_date.weekday())
        last_week = end_date - timedelta(days=end_date.weekday())
        values = []
        while cursor <= last_week:
            values.append(
                {
                    "start": cursor,
                    "label": cursor.strftime("%m/%d"),
                    "count": week_counts[cursor],
                    "precise_count": week_precise[cursor],
                    "date_count": week_date[cursor],
                    "coarse_count": week_coarse[cursor],
                }
            )
            cursor += timedelta(days=7)
        granularity = "week"

    maximum = max((item["count"] for item in values), default=0)
    for index, item in enumerate(values):
        if granularity == "day":
            posted_date = item["start"]
            item["precise_count"] = precise_counts[posted_date]
            item["date_count"] = date_counts[posted_date]
            item["coarse_count"] = coarse_counts[posted_date]
        item["height_percent"] = round(item["count"] / maximum * 100) if maximum else 0
        item["precise_percent"] = round(item["precise_count"] / item["count"] * 100) if item["count"] else 0
        item["date_percent"] = round(item["date_count"] / item["count"] * 100) if item["count"] else 0
        item["coarse_percent"] = max(0, 100 - item["precise_percent"] - item["date_percent"]) if item["count"] else 0
        item["show_label"] = index == 0 or index == len(values) - 1 or index % 4 == 0
        item["period_label"] = (
            item["start"].isoformat()
            if granularity == "day"
            else f"{item['start'].isoformat()} 起"
        )
        item.pop("start")
    return values, granularity


def _distribution(
    labels: tuple[str, ...], counts: Counter[int], total: int
) -> list[dict[str, Any]]:
    return [
        {
            "label": label,
            "count": counts[index],
            "percent": round(counts[index] / total * 100, 1) if total else 0.0,
            "width_percent": round(counts[index] / total * 100, 1) if total else 0.0,
        }
        for index, label in enumerate(labels)
    ]


def _daypart_distribution(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(item["daypart_slot"] for item in records)
    fine_counts = Counter(
        item["daypart_slot"]
        for item in records
        if item["time_unit"] in {"seconds", "minutes"}
    )
    hour_counts = Counter(
        item["daypart_slot"] for item in records if item["time_unit"] == "hours"
    )
    total = len(records)
    return [
        {
            "label": label,
            "range_label": range_label,
            "count": counts[index],
            "percent": round(counts[index] / total * 100, 1) if total else 0.0,
            "width_percent": round(counts[index] / total * 100, 1) if total else 0.0,
            "fine_count": fine_counts[index],
            "hour_count": hour_counts[index],
        }
        for index, (label, range_label) in enumerate(DAYPARTS)
    ]


def _weekday_time_heatmap(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(
        (item["two_hour_slot"], item["date"].weekday()) for item in records
    )
    maximum = max(counts.values(), default=0)
    rows = []
    for slot_index, slot_label in enumerate(TWO_HOUR_SLOT_LABELS):
        cells = []
        for weekday_index, weekday_label in enumerate(WEEKDAY_LABELS):
            count = counts[(slot_index, weekday_index)]
            level = (
                max(1, (count * 5 + maximum - 1) // maximum)
                if count and maximum
                else 0
            )
            cells.append(
                {
                    "weekday_index": weekday_index,
                    "weekday_label": weekday_label,
                    "count": count,
                    "level": level,
                }
            )
        rows.append(
            {
                "slot_index": slot_index,
                "slot_label": slot_label,
                "cells": cells,
            }
        )
    return {
        "eligible_count": len(records),
        "max_count": maximum,
        "weekday_labels": WEEKDAY_LABELS,
        "rows": rows,
    }


def _detect_patterns(
    weekday_records: list[dict[str, Any]],
    time_slot_records: list[dict[str, Any]],
    interval_records: list[dict[str, Any]],
    burst_records: list[dict[str, Any]],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    weekday_count_total = len(weekday_records)
    weekday_span = (
        (max(item["date"] for item in weekday_records) - min(item["date"] for item in weekday_records)).days
        if weekday_records else 0
    )
    if weekday_count_total >= 10 and weekday_span >= 42:
        weekday_counts = Counter(item["date"].weekday() for item in weekday_records)
        weekday, weekday_count = weekday_counts.most_common(1)[0]
        share = weekday_count / weekday_count_total
        matching_weeks = {
            item["date"].isocalendar()[:2]
            for item in weekday_records
            if item["date"].weekday() == weekday
        }
        p_value = _any_category_tail(weekday_count_total, weekday_count, 7)
        if share >= 0.50 and weekday_count >= 6 and len(matching_weeks) >= 4 and p_value <= 0.01:
            findings.append(
                {
                    "kind": "weekday",
                    "title": f"可能集中在{WEEKDAY_LABELS[weekday]}",
                    "evidence": f"{weekday_count_total} 筆日期邊界明確的回推資料中有 {weekday_count} 筆落在{WEEKDAY_LABELS[weekday]}（{share:.0%}），跨 {len(matching_weeks)} 週。",
                }
            )

    slot_count_total = len(time_slot_records)
    slot_span = (
        (max(item["date"] for item in time_slot_records) - min(item["date"] for item in time_slot_records)).days
        if time_slot_records else 0
    )
    if slot_count_total >= 10 and slot_span >= 14:
        slot_counts = Counter(item["time_slot"] for item in time_slot_records)
        slot, slot_count = slot_counts.most_common(1)[0]
        share = slot_count / slot_count_total
        matching_dates = {
            item["date"] for item in time_slot_records if item["time_slot"] == slot
        }
        p_value = _any_category_tail(slot_count_total, slot_count, len(TIME_SLOT_LABELS))
        if share >= 0.50 and slot_count >= 6 and len(matching_dates) >= 4 and p_value <= 0.01:
            findings.append(
                {
                    "kind": "time-slot",
                    "title": f"可能集中在 {TIME_SLOT_LABELS[slot]} 時段",
                    "evidence": f"{slot_count_total} 筆不跨時段邊界的回推資料中有 {slot_count} 筆落在 {TIME_SLOT_LABELS[slot]}（{share:.0%}），跨 {len(matching_dates)} 天。",
                }
            )

    active_dates = sorted({item["date"] for item in interval_records})
    active_span = (active_dates[-1] - active_dates[0]).days if active_dates else 0
    if len(active_dates) >= 8 and active_span >= 28:
        gaps = [
            (current - previous).days
            for previous, current in zip(active_dates, active_dates[1:])
        ]
        median_gap = float(median(gaps))
        if median_gap >= 2:
            deviations = [abs(gap - median_gap) for gap in gaps]
            mad = float(median(deviations))
            tolerance = max(1.0, median_gap * 0.20)
            inlier_share = sum(deviation <= tolerance for deviation in deviations) / len(gaps)
            outliers = sum(deviation > tolerance for deviation in deviations)
            if mad / median_gap <= 0.25 and inlier_share >= 0.80 and outliers <= 1:
                interval = f"{median_gap:g}"
                findings.append(
                    {
                        "kind": "interval",
                        "title": f"發文日期可能約每 {interval} 天出現",
                        "evidence": f"共 {len(active_dates)} 個發文日；{inlier_share:.0%} 的相鄰日期間隔接近中位數 {interval} 天（MAD {mad:g} 天）。",
                    }
                )

    burst_dates = [item["date"] for item in burst_records]
    burst_span = (max(burst_dates) - min(burst_dates)).days if burst_dates else 0
    if len(burst_records) >= 20 and burst_span >= 56:
        week_counts: Counter[date] = Counter()
        for item in burst_records:
            week_start = item["date"] - timedelta(days=item["date"].weekday())
            week_counts[week_start] += 1
        first_week = min(week_counts)
        last_week = max(week_counts)
        weekly: list[tuple[date, int]] = []
        cursor = first_week
        while cursor <= last_week:
            weekly.append((cursor, week_counts[cursor]))
            cursor += timedelta(days=7)
        if len(weekly) >= 8:
            values = [value for _, value in weekly]
            center = float(median(values))
            mad = float(median(abs(value - center) for value in values))
            peak_week, peak = max(weekly, key=lambda item: item[1])
            unusual = (
                0.6745 * (peak - center) / mad >= 3.5
                if mad
                else peak >= max(5, center + 4)
            )
            if unusual:
                findings.append(
                    {
                        "kind": "burst",
                        "title": "可能出現單週異常集中",
                        "evidence": f"{peak_week.isoformat()} 起的一週有 {peak} 筆；完整序列中位數為每週 {center:g} 筆。",
                    }
                )
    return findings


def _any_category_tail(sample_size: int, observed: int, categories: int) -> float:
    probability = 1 / categories
    tail = sum(
        comb(sample_size, value)
        * probability**value
        * (1 - probability) ** (sample_size - value)
        for value in range(observed, sample_size + 1)
    )
    return min(1.0, categories * tail)
