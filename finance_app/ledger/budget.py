"""Monthly allocation rules; allocations are not evidence of available cash."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class BucketKind(StrEnum):
    FIXED_EXPENSE = "fixed_expense"
    RESERVE = "reserve"
    INVESTMENT = "investment"
    DISCRETIONARY = "discretionary"


class BudgetMismatch(ValueError):
    """The allocation does not match the salary."""


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
