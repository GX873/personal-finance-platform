"""Period selection and risk metrics for fund NAV histories."""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finance_app.market.xalpha_adapter import XalphaAdapter


class Period(StrEnum):
    ONE_MONTH = "1m"
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ONE_YEAR = "1y"
    ALL = "all"


@dataclass(frozen=True)
class FundMetrics:
    total_return: Decimal | None
    max_drawdown: Decimal | None
    annualized_volatility: Decimal | None
    risk_label: str | None


def period_start(period: Period, end: date) -> date | None:
    """Return the inclusive calendar cutoff for a period ending on ``end``."""
    period = Period(period)
    months = {
        Period.ONE_MONTH: 1,
        Period.THREE_MONTHS: 3,
        Period.SIX_MONTHS: 6,
        Period.ONE_YEAR: 12,
    }.get(period)
    if months is None:
        return None
    month_index = end.year * 12 + end.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(end.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def calculate_metrics(values: Sequence[Decimal | int | float]) -> FundMetrics:
    """Calculate official-NAV metrics without exposing xalpha implementation types."""
    if not values:
        return FundMetrics(None, None, None, None)
    adapter = XalphaAdapter(values)
    total_return = adapter.total_return()
    max_drawdown = adapter.max_drawdown()
    volatility = adapter.volatility()
    if volatility is None or max_drawdown is None:
        label = None
    elif volatility <= Decimal("0.10") and max_drawdown >= Decimal("-0.10"):
        label = "较低"
    elif volatility <= Decimal("0.20") and max_drawdown >= Decimal("-0.20"):
        label = "中等"
    else:
        label = "较高"
    return FundMetrics(total_return, max_drawdown, volatility, label)
