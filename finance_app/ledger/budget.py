"""Monthly allocation rules; allocations are not evidence of available cash."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class BucketKind(StrEnum):
    FIXED_EXPENSE = "fixed_expense"
    RESERVE = "reserve"
    INVESTMENT = "investment"
    DISCRETIONARY = "discretionary"


class BudgetMismatch(ValueError):
    """The allocation does not match the salary."""


@dataclass(frozen=True)
class CycleAllocation:
    reserve_replenishment_cents: int
    normal_investment_cents: int
    special_opportunity_cents: int


def validate_cents(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("money must be a nonnegative integer number of cents")


@dataclass(frozen=True)
class BudgetRule:
    fixed_expense_cents: int = 270000
    reserve_cents: int = 100000
    investment_cents: int = 20000
    discretionary_cents: int = 10000

    def __post_init__(self) -> None:
        for kind in BucketKind:
            validate_cents(getattr(self, f"{kind.value}_cents"))


def default_budget_rule() -> BudgetRule:
    """Return the default allocation for a 4000 yuan monthly salary."""
    return BudgetRule()


def salary_cycle(day: date) -> tuple[date, date]:
    """Return the salary cycle containing ``day`` (15th through 14th)."""
    if type(day) is not date:
        raise ValueError("day must be a date")
    if day.day >= 15:
        start = day.replace(day=15)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        previous_month = (day.replace(day=1) - timedelta(days=1)).replace(day=1)
        start = previous_month.replace(day=15)
        next_month = day.replace(day=1)
    return start, next_month + timedelta(days=13)


def special_opportunity_budget(
    reserve_cents: int,
    normal_budget_cents: int,
    *,
    used: bool,
    reserve_target_cents: int = 100000,
    reserve_floor_cents: int = 90000,
) -> int:
    """Return the one-time reserve top-up available for a special opportunity."""
    for value in (
        reserve_cents,
        normal_budget_cents,
        reserve_target_cents,
        reserve_floor_cents,
    ):
        validate_cents(value)
    if reserve_floor_cents > reserve_target_cents or used:
        return 0
    if reserve_cents < reserve_target_cents:
        return 0
    return min(10000, reserve_cents - reserve_floor_cents)


def cycle_allocation(
    *,
    reserve_cents: int,
    reserve_target_cents: int = 100000,
    normal_budget_cents: int = 20000,
    special_used: bool,
) -> CycleAllocation:
    """Apply reserve replenishment before restoring investment budgets."""
    for value in (reserve_cents, reserve_target_cents, normal_budget_cents):
        validate_cents(value)
    replenishment = max(0, reserve_target_cents - reserve_cents)
    if replenishment:
        return CycleAllocation(replenishment, 0, 0)
    return CycleAllocation(
        0,
        normal_budget_cents,
        special_opportunity_budget(
            reserve_cents,
            normal_budget_cents,
            used=special_used,
            reserve_target_cents=reserve_target_cents,
        ),
    )


def allocate_salary(salary_cents: int, rule: BudgetRule) -> dict[BucketKind, int]:
    """Allocate a salary exactly; never borrow from reserve to fill a shortfall."""
    validate_cents(salary_cents)
    amounts = {kind: getattr(rule, f"{kind.value}_cents") for kind in BucketKind}
    if sum(amounts.values()) != salary_cents:
        raise BudgetMismatch("allocated total must equal salary_cents")
    return amounts


def investable_cents(confirmed_balances: Mapping[BucketKind, int]) -> int:
    """Read actual confirmed investment cash, never monthly plans or reserve.

    The caller must supply balances reconciled to received cash and spending;
    an allocation returned by allocate_salary is not a confirmed balance.
    """
    amount = confirmed_balances.get(BucketKind.INVESTMENT, 0)
    validate_cents(amount)
    return amount
