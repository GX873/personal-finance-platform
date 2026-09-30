from datetime import date
from decimal import Decimal

from finance_app.market.analytics import (
    FundMetrics,
    Period,
    calculate_metrics,
    period_start,
)
from finance_app.market.xalpha_adapter import XalphaAdapter


def test_period_start_uses_calendar_months_and_all_has_no_cutoff():
    end = date(2026, 9, 29)
    assert period_start(Period.ONE_MONTH, end) == date(2026, 8, 29)
    assert period_start(Period.THREE_MONTHS, end) == date(2026, 6, 29)
    assert period_start(Period.SIX_MONTHS, end) == date(2026, 3, 29)
    assert period_start(Period.ONE_YEAR, end) == date(2025, 9, 29)
    assert period_start(Period.ALL, end) is None


def test_metrics_are_annualized_and_classified():
    metrics = calculate_metrics(
        [Decimal("1.00"), Decimal("1.02"), Decimal("0.99"), Decimal("1.05")]
    )
    assert isinstance(metrics, FundMetrics)
    assert metrics.total_return == Decimal("0.05")
    assert metrics.max_drawdown < 0
    assert metrics.annualized_volatility > 0
    assert metrics.risk_label in {"较低", "中等", "较高"}


def test_insufficient_history_is_missing_not_zero():
    adapter = XalphaAdapter([Decimal("1.00")])
    assert adapter.total_return() is None
    assert adapter.max_drawdown() is None
    assert adapter.volatility() is None
