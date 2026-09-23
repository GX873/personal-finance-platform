from collections.abc import Iterator
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
    investable_cents,
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
    allocation = allocate_salary(400000, BudgetRule())
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
