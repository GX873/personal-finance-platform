from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from finance_app.ledger.models import RiskLevel
from finance_app.portfolio.rules import (
    AdviceAction,
    RuleContext,
    evaluate_allocation,
    evaluate_new_investment,
    nav_is_fresh,
)

NOW = datetime(2026, 9, 21, 8, tzinfo=timezone(timedelta(hours=8)))


def context():
    return RuleContext(100000, 20000, True, True, True, timestamp=NOW)


@pytest.mark.parametrize("field", ["reserve_cents", "investment_cash_cents"])
def test_unknown_money_blocks(field):
    advice = evaluate_new_investment(
        replace(context(), **{field: None}), RiskLevel.HIGH
    )
    assert (advice.action, advice.amount_cents) == (AdviceAction.WAIT_FOR_DATA, None)


@pytest.mark.parametrize("field", ["prices_fresh", "holdings_fresh", "cash_fresh"])
def test_stale_data_blocks(field):
    assert (
        evaluate_new_investment(
            replace(context(), **{field: False}), RiskLevel.LOW
        ).action
        == AdviceAction.WAIT_FOR_DATA
    )


def test_reserve_cash_budget_and_conditional_purchase():
    for field, value in [
        ("reserve_cents", 99999),
        ("investment_cash_cents", 0),
        ("month_budget_remaining_cents", 0),
    ]:
        advice = evaluate_new_investment(
            replace(context(), **{field: value}), RiskLevel.HIGH
        )
        assert (advice.action, advice.amount_cents) == (AdviceAction.WAIT, 0)
    advice = evaluate_new_investment(
        replace(context(), investment_cash_cents=12345), RiskLevel.HIGH
    )
    assert advice.action == AdviceAction.BUY
    assert advice.amount_cents == 12345 and advice.conditional
    assert advice.max_risk and advice.stop_discipline and advice.source


def test_allocation_limits_and_unconfigured():
    assert (
        evaluate_allocation(context(), 2500, 100000, 1000, 1500).amount_cents == 10000
    )
    assert evaluate_allocation(context(), 1501, 100000, 1000, 1500).amount_cents == 66
    assert (
        evaluate_allocation(context(), 100, 100000, None, None).action
        == AdviceAction.HOLD
    )
    with pytest.raises(ValueError):
        evaluate_allocation(context(), 2500, 100000, 2000, 1500)


def test_configurable_batch_and_future_context():
    assert (
        evaluate_allocation(
            context(), 2500, 100000, 1000, 1500, batch_bps=500
        ).amount_cents
        == 5000
    )
    assert (
        evaluate_new_investment(
            context(), RiskLevel.HIGH, now=NOW - timedelta(seconds=1)
        ).action
        == AdviceAction.WAIT_FOR_DATA
    )
    with pytest.raises(ValueError):
        replace(context(), prices_fresh="yes")


def test_nav_weekend_morning_evening_future_and_holiday():
    friday = date(2026, 9, 18)
    assert nav_is_fresh(friday, NOW, NOW)
    assert nav_is_fresh(friday, NOW - timedelta(days=1), NOW - timedelta(days=1))
    assert not nav_is_fresh(friday, NOW, NOW.replace(hour=22))
    assert not nav_is_fresh(NOW.date() + timedelta(days=1), NOW, NOW)
    assert not nav_is_fresh(friday, NOW + timedelta(seconds=1), NOW)
    assert nav_is_fresh(date(2026, 9, 17), NOW, NOW, holidays=frozenset({friday}))
    with pytest.raises(ValueError):
        nav_is_fresh(friday, NOW, NOW, holidays=frozenset({"bad"}))
