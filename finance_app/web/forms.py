"""Strict parsers and redacted audit helpers for manually entered finance data."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from finance_app.auth.models import User
from finance_app.ledger.models import AuditEvent

SHANGHAI = ZoneInfo("Asia/Shanghai")
_YUAN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?")
_SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}")


class FormError(ValueError):
    """An error safe to display beside a manual-entry form."""


def _text(value: object, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FormError(f"{label}不能为空，且首尾不能有空格。")
    if len(value) > maximum:
        raise FormError(f"{label}过长。")
    return value


def parse_yuan(value: object, label: str, *, positive: bool = False) -> int:
    """Parse exact, plain yuan text to integer cents without rounding."""
    if type(value) is not str or _YUAN.fullmatch(value) is None:
        raise FormError(f"{label}须为最多两位小数的人民币金额。")
    whole, dot, fraction = value.partition(".")
    cents = int(whole) * 100 + int((fraction + "00")[:2] if dot else "00")
    if positive and cents == 0:
        raise FormError(f"{label}必须大于 0。")
    if cents > 2**63 - 1:
        raise FormError(f"{label}超出可记录范围。")
    return cents


def parse_decimal(value: object, label: str, *, positive: bool = False) -> Decimal:
    """Parse a finite plain decimal with at most eight fractional places."""
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:
        raise FormError(f"{label}须为最多八位小数的非负数字。")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FormError(f"{label}格式无效。") from exc
    if positive and parsed == 0:
        raise FormError(f"{label}必须大于 0。")
    if parsed > Decimal(1000000):
        raise FormError(f"{label}超出可记录范围。")
    return parsed


def parse_id(value: object, label: str, *, optional: bool = False) -> int | None:
    if optional and value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise FormError(f"请选择有效的{label}。")
    return int(value)


def parse_local_datetime(value: object) -> datetime:
    text = _text(value, "发生时间", maximum=16)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", text) is None:
        raise FormError("发生时间格式无效。")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise FormError("发生时间格式无效。") from exc
    return parsed.replace(tzinfo=SHANGHAI)


def parse_date(value: object, label: str) -> date:
    text = _text(value, label, maximum=10)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FormError(f"{label}格式无效。") from exc


def manual_source(value: object) -> str:
    if value == "":
        return "manual:user-entry"
    text = _text(value, "来源", maximum=55)
    if text.startswith("manual:"):
        text = text.removeprefix("manual:")
    if text.lower() in {"manual", "realtime", "real-time", "live"}:
        raise FormError("来源必须具体说明人工价格的依据。")
    if _SOURCE.fullmatch(text) is None:
        raise FormError("来源只能包含字母、数字、点、下划线或连字符。")
    return f"manual:{text}"


def required_text(value: object, label: str, *, maximum: int = 200) -> str:
    return _text(value, label, maximum=maximum)


def audit_event(
    *,
    user: User,
    event_type: str,
    action: str,
    entity_type: str,
    entity_id: int,
    summary: dict[str, Any],
) -> AuditEvent:
    """Build a web audit event from an explicit, non-secret summary allowlist."""
    details = {
        "actor": {"id": user.id, "username": user.username},
        "action": action,
        "entity": {"type": entity_type, "id": entity_id},
        "request_id": str(uuid4()),
        "summary": summary,
    }
    return AuditEvent(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json.dumps(details, ensure_ascii=False, default=str),
    )
