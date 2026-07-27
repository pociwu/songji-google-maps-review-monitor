from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo


SHORT_UNITS = {"seconds", "minutes", "hours", "days"}
LONG_UNITS = {"weeks", "months", "years"}


@dataclass(frozen=True, slots=True)
class RelativeTime:
    value: int
    unit: str

    @property
    def kind(self) -> str:
        if self.unit in SHORT_UNITS and not (self.unit == "days" and self.value >= 7):
            return "short"
        if self.unit in LONG_UNITS:
            return "long"
        return "unparsed"


_UNIT_ALIASES = {
    "秒": "seconds", "秒鐘": "seconds", "秒钟": "seconds",
    "分鐘": "minutes", "分钟": "minutes", "分": "minutes",
    "小時": "hours", "小时": "hours", "時間": "hours",
    "天": "days", "日": "days",
    "週": "weeks", "周": "weeks", "週間": "weeks",
    "月": "months", "個月": "months", "个月": "months", "か月": "months", "ヶ月": "months",
    "年": "years",
    "second": "seconds", "seconds": "seconds",
    "minute": "minutes", "minutes": "minutes",
    "hour": "hours", "hours": "hours",
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
    "month": "months", "months": "months",
    "year": "years", "years": "years",
}


def parse_relative_time(text: str) -> RelativeTime | None:
    value = " ".join((text or "").strip().lower().split())
    if not value:
        return None
    if value in {"剛剛", "刚刚", "現在", "现在", "たった今", "just now"}:
        return RelativeTime(0, "seconds")
    if value in {"昨天", "昨日", "yesterday"}:
        return RelativeTime(1, "days")

    match = re.search(
        r"(\d+)\s*(秒鐘?|秒钟|分鐘|分钟|分|小時|小时|時間|天|日|週|周|週間|個月|个月|か月|ヶ月|月|年)\s*前",
        value,
    )
    if match:
        return RelativeTime(int(match.group(1)), _UNIT_ALIASES[match.group(2)])

    match = re.search(
        r"\b(\d+|a|an)\s+(second|minute|hour|day|week|month|year)s?\s+ago\b",
        value,
    )
    if match:
        number = 1 if match.group(1) in {"a", "an"} else int(match.group(1))
        return RelativeTime(number, _UNIT_ALIASES[match.group(2)])
    return None


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("時間必須包含時區")
    return parsed


def back_calculate(relative: RelativeTime, observed_at: str, timezone_name: str) -> str | None:
    if relative.kind != "short":
        return None
    observed = parse_datetime(observed_at).astimezone(ZoneInfo(timezone_name))
    delta = {
        "seconds": timedelta(seconds=relative.value),
        "minutes": timedelta(minutes=relative.value),
        "hours": timedelta(hours=relative.value),
        "days": timedelta(days=relative.value),
    }[relative.unit]
    return (observed - delta).isoformat()


def estimate_from_transition(
    previous_observed_at: str,
    current_observed_at: str,
    current_relative: RelativeTime,
    timezone_name: str,
) -> str | None:
    if current_relative.kind != "long":
        return None
    previous = parse_datetime(previous_observed_at)
    current = parse_datetime(current_observed_at)
    midpoint = previous + (current - previous) / 2
    local_midpoint = midpoint.astimezone(ZoneInfo(timezone_name))
    if current_relative.unit == "weeks":
        result = local_midpoint - timedelta(weeks=current_relative.value)
    elif current_relative.unit == "months":
        result = _subtract_months(local_midpoint, current_relative.value)
    else:
        result = _subtract_months(local_midpoint, current_relative.value * 12)
    return result.date().isoformat()


def _subtract_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)
