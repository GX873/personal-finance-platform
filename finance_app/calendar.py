"""Small, explicit calendar helpers for reminder scheduling."""

from __future__ import annotations

import json
from datetime import date
from typing import Any


def is_skipped_reminder_day(day: date, holidays: frozenset[date]) -> bool:
    if type(day) is not date:
        raise ValueError("day must be a date")
    if any(type(item) is not date for item in holidays):
        raise ValueError("holidays must contain dates")
    return day.weekday() >= 5 or day in holidays


def parse_holidays(value: str) -> frozenset[date]:
    try:
        payload: Any = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("holiday configuration must be a JSON array") from exc
    if not isinstance(payload, list):
        raise ValueError("holiday configuration must be a JSON array")  # noqa: TRY004
    try:
        result = frozenset(date.fromisoformat(item) for item in payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("holiday dates must use YYYY-MM-DD") from exc
    return result


def serialize_holidays(holidays: frozenset[date]) -> str:
    if any(type(item) is not date for item in holidays):
        raise ValueError("holidays must contain dates")
    return json.dumps(sorted(item.isoformat() for item in holidays), ensure_ascii=False)
