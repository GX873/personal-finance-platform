from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from finance_app.db import create_db_engine
from finance_app.ledger.budget import (
    BucketKind,
    BudgetMismatch,
    BudgetRule,
    allocate_salary,
    cycle_allocation,
    default_budget_rule,
    investable_cents,
    salary_cycle,
    special_opportunity_budget,
)
from finance_app.ledger.models import CashBucket, MonthlyBudget, Transaction
from finance_app.ledger.service import BudgetConflict, post_salary_month


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    url = f"sqlite:///{tmp_path / 'budget.db'}"
    monkeypatch.setenv("FINANCE_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    try:
        with Session(engine) as value:
            yield value
    finally:
        engine.dispose()


def test_default_allocation_and_confirmed_investment():
    allocation = allocate_salary(400000, default_budget_rule())
    assert allocation == {
        "fixed_expense": 270000,
        "reserve": 100000,
        "investment": 20000,
        "discretionary": 10000,
    }
    assert investable_cents({BucketKind.RESERVE: 999999}) == 0
    assert (
        investable_cents({BucketKind.INVESTMENT: 123, BucketKind.RESERVE: 999999})
        == 123
    )
    with pytest.raises(BudgetMismatch, match="allocated total"):
        allocate_salary(399999, BudgetRule())


def test_salary_cycle_starts_on_fifteenth_and_ends_before_next_fifteenth():
    assert salary_cycle(date(2026, 9, 15)) == (date(2026, 9, 15), date(2026, 10, 14))
    assert salary_cycle(date(2026, 10, 14)) == (date(2026, 9, 15), date(2026, 10, 14))
    assert salary_cycle(date(2026, 10, 15)) == (date(2026, 10, 15), date(2026, 11, 14))


def test_replenishment_blocks_investment_and_special_budget_until_reserve_is_full():
    allocation = cycle_allocation(
        reserve_cents=90000,
        reserve_target_cents=100000,
        normal_budget_cents=20000,
        special_used=False,
    )
    assert allocation.reserve_replenishment_cents == 10000
    assert allocation.normal_investment_cents == 0
    assert allocation.special_opportunity_cents == 0


def test_special_opportunity_is_limited_to_one_hundred_yuan_once_per_cycle():
    assert special_opportunity_budget(100000, 20000, used=False) == 10000
    assert special_opportunity_budget(100000, 20000, used=True) == 0


@pytest.mark.parametrize("value", [-1, True, 1.5, "20000"])
def test_money_validation(value):
    with pytest.raises(ValueError):
        BudgetRule(investment_cents=value)
    with pytest.raises(ValueError):
        allocate_salary(value, BudgetRule())
    with pytest.raises(ValueError):
        investable_cents({BucketKind.INVESTMENT: value})


@pytest.mark.parametrize(
    "month", ["2026-00", "2026-13", "2026-1", "0000-01", "2026-01\n", 202601]
)
def test_month_validation(session, month):
    with pytest.raises(ValueError):
        post_salary_month(session, month, 400000, BudgetRule())
    assert session.scalars(select(MonthlyBudget)).all() == []


def test_post_idempotence_conflicts_and_cash_separation(session):
    reserve = CashBucket(bucket_kind="reserve", balance_cents=500000)
    session.add(reserve)
    session.commit()
    rows = post_salary_month(session, "2026-09", 400000, BudgetRule())
    session.commit()
    again = post_salary_month(session, "2026-09", 400000, BudgetRule())
    assert len(rows) == 4
    assert [row.id for row in rows] == [row.id for row in again]
    with pytest.raises(BudgetConflict):
        post_salary_month(
            session, "2026-09", 400000, BudgetRule(260000, 110000, 20000, 10000)
        )
    with pytest.raises(BudgetConflict):
        post_salary_month(
            session, "2026-09", 410000, BudgetRule(280000, 100000, 20000, 10000)
        )
    with pytest.raises(BudgetConflict):
        post_salary_month(session, "2026-09", 410000, BudgetRule())
    assert session.scalars(select(CashBucket)).all() == [reserve]
    assert reserve.balance_cents == 500000
    assert session.scalars(select(Transaction)).all() == []


def test_partial_month_conflicts(session):
    session.add(
        MonthlyBudget(month="2026-09", bucket_kind="reserve", amount_cents=100000)
    )
    session.commit()
    with pytest.raises(BudgetConflict):
        post_salary_month(session, "2026-09", 400000, BudgetRule())
    assert len(session.scalars(select(MonthlyBudget)).all()) == 1


def test_caller_rollback_reverts_complete_allocation(session):
    post_salary_month(session, "2026-09", 400000, BudgetRule())
    session.rollback()
    assert session.scalars(select(MonthlyBudget)).all() == []


def test_mid_insert_failure_is_atomic_and_session_usable(session):
    session.execute(
        text(
            "CREATE TRIGGER fail_investment BEFORE INSERT ON monthly_budgets "
            "WHEN NEW.bucket_kind = 'investment' BEGIN "
            "SELECT RAISE(ABORT, 'test failure'); END"
        )
    )
    session.commit()
    with pytest.raises(IntegrityError):
        post_salary_month(session, "2026-09", 400000, BudgetRule())
    assert session.scalars(select(MonthlyBudget)).all() == []
    session.add(CashBucket(bucket_kind="reserve", balance_cents=5))
    session.commit()
