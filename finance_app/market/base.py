from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


def validate_storable_nav(value: Decimal) -> None:
    """Require an exact positive value within Numeric(24, 8)."""
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("NAV must be a finite positive Decimal")
    digits = list(value.as_tuple().digits)
    exponent_value = value.as_tuple().exponent
    if not isinstance(exponent_value, int):
        raise TypeError("finite NAV exponent must be an integer")
    exponent = exponent_value
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    fractional_digits = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    if fractional_digits > 8 or integer_digits > 16:
        raise ValueError("NAV must be exactly representable as Numeric(24, 8)")


@dataclass(frozen=True)
class FundNavQuote:
    value: Decimal
    valuation_date: date
    source: str
    source_url: str
    fetched_at: datetime
    attempts: int = 1

    def __post_init__(self) -> None:
        validate_storable_nav(self.value)
        if type(self.valuation_date) is not date:
            raise ValueError("valuation date must be a date")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("quote source is required")
        if not isinstance(self.source_url, str) or not self.source_url.startswith(
            "https://"
        ):
            raise ValueError("quote source URL must use HTTPS")
        if (
            not isinstance(self.fetched_at, datetime)
            or self.fetched_at.utcoffset() is None
        ):
            raise ValueError("fetch time must be timezone-aware")
        if type(self.attempts) is not int or self.attempts < 1:
            raise ValueError("attempt count must be a positive integer")


class MarketDataError(RuntimeError):
    """A safe-to-display market-data failure without upstream response content."""

    def __init__(
        self,
        *,
        code: str,
        source: str,
        source_url: str,
        fetched_at: datetime,
        attempts: int,
        summary: str,
    ) -> None:
        super().__init__(summary)
        self.code = code
        self.source = source
        self.source_url = source_url
        self.fetched_at = fetched_at
        self.attempts = attempts
        self.summary = summary


class FundNavProvider(Protocol):
    source: str
    source_url: str

    def fetch(self, fund_code: str) -> FundNavQuote: ...
