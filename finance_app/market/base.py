from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

MAX_NAV = Decimal(1000000)


class QuoteType(StrEnum):
    OFFICIAL_NAV = "official_nav"
    INTRADAY_ESTIMATE = "intraday_estimate"


def validate_storable_nav(value: Decimal) -> None:
    """Apply the SQLite-safe decimal boundary shared with ledger quantities."""
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value <= 0
        or value > MAX_NAV
        or int(value.as_tuple().exponent) < -8
    ):
        raise ValueError(
            "NAV must be finite, positive, <= 1000000 with <= 8 places"
        )


@dataclass(frozen=True)
class FundNavQuote:
    value: Decimal
    valuation_date: date
    source: str
    source_url: str
    fetched_at: datetime
    attempts: int = 1
    quote_type: QuoteType = QuoteType.OFFICIAL_NAV

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
        QuoteType(self.quote_type)


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


class IntradayEstimateProvider(FundNavProvider, Protocol):
    """Provider whose fetch result must be an intraday estimate."""
