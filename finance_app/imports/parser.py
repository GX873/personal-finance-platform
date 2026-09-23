"""Strict CSV/XLSX portfolio parsing without any persistence side effects."""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path

from openpyxl import load_workbook  # type: ignore[import-untyped]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".png", ".jpg", ".jpeg"}

_ALIASES = {
    "基金代码": "asset_code",
    "代码": "asset_code",
    "基金名称": "asset_name",
    "名称": "asset_name",
    "份额": "quantity",
    "成本金额": "cost",
    "持仓成本": "cost",
    "当前市值": "market_value",
    "持有金额": "market_value",
    "可用现金": "available_cash",
    "日期": "date",
}
_MONEY = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?")
_QUANTITY = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?")
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MAX_CENTS = 2**63 - 1


class ImportFileError(ValueError):
    """An upload or workbook error safe to show in the import form."""


@dataclass
class PortfolioRow:
    asset_code: str = ""
    asset_name: str = ""
    quantity: Decimal | None = None
    cost_cents: int | None = None
    market_value_cents: int | None = None
    available_cash_cents: int | None = None
    occurred_on: date | None = None
    source_text: str = ""
    confidence: float | None = None
    errors: list[str] = field(default_factory=list)
    editable_values: dict[str, str] = field(default_factory=dict)

    def form_values(self) -> dict[str, str]:
        return self.editable_values or {
            "asset_code": self.asset_code,
            "asset_name": self.asset_name,
            "quantity": "" if self.quantity is None else str(self.quantity),
            "cost": _format_cents(self.cost_cents),
            "market_value": _format_cents(self.market_value_cents),
            "available_cash": _format_cents(self.available_cash_cents),
            "date": "" if self.occurred_on is None else self.occurred_on.isoformat(),
        }


def _format_cents(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value // 100}.{value % 100:02d}"


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _money(value: str, label: str, errors: list[str]) -> int | None:
    if value == "":
        return None
    if _MONEY.fullmatch(value) is None:
        errors.append(f"{label}必须是最多两位小数的非负金额")
        return None
    whole, dot, fraction = value.partition(".")
    cents = int(whole) * 100 + int((fraction + "00")[:2] if dot else "00")
    if cents > _MAX_CENTS:
        errors.append(f"{label}超出可记录范围")
        return None
    return cents


def _quantity(value: str, errors: list[str]) -> Decimal | None:
    if value == "":
        return None
    if _QUANTITY.fullmatch(value) is None:
        errors.append("份额必须是最多八位小数的非负数字")
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        errors.append("份额格式无效")
        return None
    if parsed > Decimal(1000000):
        errors.append("份额超出可记录范围")
        return None
    return parsed


def _date(value: str, errors: list[str]) -> date | None:
    if value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append("日期必须是 YYYY-MM-DD")
        return None


def normalize_row(
    values: Mapping[str, object],
    *,
    source_text: str = "",
    confidence: float | None = None,
) -> PortfolioRow:
    """Validate editable normalized fields into exact ledger-ready values."""
    text = {key: _cell_text(value) for key, value in values.items()}
    errors: list[str] = []
    code = text.get("asset_code", "")
    name = text.get("asset_name", "")
    quantity = _quantity(text.get("quantity", ""), errors)
    cost = _money(text.get("cost", ""), "成本金额", errors)
    market_value = _money(text.get("market_value", ""), "当前市值", errors)
    available_cash = _money(text.get("available_cash", ""), "可用现金", errors)
    occurred_on = _date(text.get("date", ""), errors)

    has_asset = any((code, name, text.get("quantity", ""), text.get("cost", "")))
    if has_asset:
        if _CODE.fullmatch(code) is None:
            errors.append("基金代码格式无效")
        if not name or len(name) > 200:
            errors.append("基金名称不能为空且不能超过 200 字符")
        if quantity is None or quantity <= 0:
            errors.append("份额必须大于 0")
        if cost is None or cost <= 0:
            errors.append("持仓成本必须大于 0")
    if available_cash is not None and available_cash < 0:
        errors.append("可用现金不能为负")
    if not has_asset and available_cash is None:
        errors.append("每行必须包含持仓或可用现金")
    if occurred_on is None:
        errors.append("日期不能为空")

    return PortfolioRow(
        asset_code=code,
        asset_name=name,
        quantity=quantity,
        cost_cents=cost,
        market_value_cents=market_value,
        available_cash_cents=available_cash,
        occurred_on=occurred_on,
        source_text=source_text,
        confidence=confidence,
        errors=list(dict.fromkeys(errors)),
        editable_values=text,
    )


def _normalized_values(raw: dict[str, object]) -> dict[str, object]:
    values: dict[str, object] = {}
    for heading, value in raw.items():
        normalized = _ALIASES.get(_cell_text(heading).replace(" ", ""))
        if normalized is not None:
            values[normalized] = value
    return values


def _row(raw: dict[str, object]) -> PortfolioRow:
    source_text = " | ".join(
        f"{_cell_text(key)}: {_cell_text(value)}" for key, value in raw.items()
    )
    return normalize_row(_normalized_values(raw), source_text=source_text)


def _csv_rows(content: bytes) -> list[dict[str, object]]:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportFileError("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(StringIO(decoded))
    if reader.fieldnames is None:
        raise ImportFileError("CSV is missing a header row")
    return [
        dict(row) for row in reader if any(_cell_text(value) for value in row.values())
    ]


def _xlsx_rows(content: bytes) -> list[dict[str, object]]:
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        headers = [_cell_text(value) for value in next(iterator)]
        rows = [
            dict(zip(headers, values, strict=False))
            for values in iterator
            if any(_cell_text(value) for value in values)
        ]
        workbook.close()
        return rows
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        raise ImportFileError("XLSX workbook could not be read") from exc


def parse_portfolio_file(content: bytes, filename: str) -> list[PortfolioRow]:
    """Parse an allowed structured upload; image files are handled by OCR."""
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ImportFileError("unsupported import file type")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ImportFileError("upload exceeds the 10 MiB limit")
    if extension not in {".csv", ".xlsx"}:
        raise ImportFileError("image uploads must be parsed by OCR")
    raw_rows = _csv_rows(content) if extension == ".csv" else _xlsx_rows(content)
    if not raw_rows:
        raise ImportFileError("import file contains no data rows")
    return [_row(raw) for raw in raw_rows]
