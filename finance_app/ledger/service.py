"""Transactional budget allocation services."""

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.ledger.budget import (
    BudgetMismatch,
    BudgetRule,
    allocate_salary,
    validate_cents,
)
from finance_app.ledger.models import MonthlyBudget
from finance_app.ledger.transactions import (
    TransactionConflict,
    account_cash_balance,
    calculate_position,
    post_transaction,
    rebuild_position,
    reverse_transaction,
)

__all__ = [
    "BudgetConflict",
    "TransactionConflict",
    "account_cash_balance",
    "calculate_position",
    "post_salary_month",
    "post_transaction",
    "rebuild_position",
    "reverse_transaction",
]


class BudgetConflict(ValueError):
    """A month already contains a different or incomplete allocation."""


def post_salary_month(
    session: Session, month: str, salary_cents: int, rule: BudgetRule
) -> list[MonthlyBudget]:
    """Record allocations after the caller confirms salary receipt.

    This records planning amounts only: actual account transactions and cash
    bucket balances must be reconciled separately. No accumulated reserve is
    changed. Flush without committing; the caller owns commit or rollback.
    Repeating exactly the same complete allocation is idempotent.
    """
    if not isinstance(month, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) is None:
        raise ValueError("month must be YYYY-MM")
    date(int(month[:4]), int(month[5:]), 1)
    validate_cents(salary_cents)

    connection = session.connection()
    if connection.dialect.name == "sqlite":
        # SQLite legacy transaction mode does not begin for SELECT or SAVEPOINT.
        # Begin the outer transaction so releasing our savepoint cannot commit it.
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")

    with session.begin_nested():
        existing = list(
            session.scalars(select(MonthlyBudget).where(MonthlyBudget.month == month))
        )
        try:
            allocation = allocate_salary(salary_cents, rule)
        except BudgetMismatch as exc:
            if existing:
                raise BudgetConflict(
                    f"month {month} already has a different allocation"
                ) from exc
            raise
        if existing:
            actual = {row.bucket_kind: row.amount_cents for row in existing}
            if len(existing) != len(allocation) or actual != allocation:
                raise BudgetConflict(
                    f"month {month} already has a different allocation"
                )
            by_kind = {row.bucket_kind: row for row in existing}
            return [by_kind[kind] for kind in allocation]
        rows = [
            MonthlyBudget(month=month, bucket_kind=kind, amount_cents=amount)
            for kind, amount in allocation.items()
        ]
        session.add_all(rows)
        session.flush()
        return rows
