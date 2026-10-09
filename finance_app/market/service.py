from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.db import utc_now
from finance_app.ledger.models import Asset, AuditEvent
from finance_app.market.base import (
    FundNavProvider,
    MarketDataError,
    QuoteType,
)
from finance_app.portfolio.models import PriceSnapshot
from finance_app.portfolio.rules import SHANGHAI, nav_is_fresh
from finance_app.portfolio.service import (
    price_selection_key,
    refresh_current_snapshot_after_price_update,
)


class RefreshStatus(StrEnum):
    SUCCESS = "success"
    STALE = "stale"
    FAILED = "failed"


@dataclass(frozen=True)
class RefreshResult:
    status: RefreshStatus
    is_fresh: bool
    snapshot: PriceSnapshot | None
    last_good_snapshot: PriceSnapshot | None
    last_good_price: Decimal | None
    source: str
    source_url: str
    fetched_at: datetime
    attempts: int
    error_summary: str | None
    provider_attempts: tuple[ProviderAttempt, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProviderAttempt:
    source: str
    status: RefreshStatus
    error_summary: str | None


class FundPriceService:
    """Refresh a known fund asset; the caller owns the database transaction."""

    def __init__(
        self,
        session: Session,
        provider: FundNavProvider,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._provider = provider
        self._clock = clock

    def refresh(
        self,
        fund_code: str,
        *,
        required_quote_type: QuoteType | None = None,
    ) -> RefreshResult:
        asset = self._session.scalar(
            select(Asset).where(
                Asset.code == fund_code,
                Asset.market == "CN",
                Asset.asset_class == "fund",
            )
        )
        if asset is None:
            reference = self._clock()
            return RefreshResult(
                status=RefreshStatus.FAILED,
                is_fresh=False,
                snapshot=None,
                last_good_snapshot=None,
                last_good_price=None,
                source=self._provider.source,
                source_url=self._provider.source_url,
                fetched_at=reference,
                attempts=0,
                error_summary=f"asset_not_found: fund asset {fund_code} was not found",
            )

        requested_quote_type = required_quote_type or QuoteType.OFFICIAL_NAV
        last_good = self._last_good(asset.id, requested_quote_type)
        try:
            quote = self._provider.fetch(fund_code)
        except Exception as error:  # noqa: BLE001 - provider fetch boundary
            if isinstance(error, MarketDataError):
                source = error.source
                source_url = error.source_url
                fetched_at = error.fetched_at
                attempts = error.attempts
                error_code = error.code
                summary = error.summary
            else:
                source = self._provider.source
                source_url = self._provider.source_url
                fetched_at = self._clock()
                attempts = 1
                error_code = "provider_exception"
                summary = str(error)[:240] or "provider failed"
            self._session.add(
                AuditEvent(
                    event_type="market_price_refresh_failed",
                    entity_type="asset",
                    entity_id=asset.id,
                    details_json=json.dumps(
                        {
                            "source": source,
                            "source_url": source_url,
                            "fetched_at": fetched_at.isoformat(),
                            "attempts": attempts,
                            "error_code": error_code,
                            "error_text": summary,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
            )
            self._session.flush()
            return RefreshResult(
                status=RefreshStatus.FAILED,
                is_fresh=False,
                snapshot=None,
                last_good_snapshot=last_good,
                last_good_price=last_good.price if last_good is not None else None,
                source=source,
                source_url=source_url,
                fetched_at=fetched_at,
                attempts=attempts,
                error_summary=summary,
            )

        if (
            required_quote_type is not None
            and QuoteType(quote.quote_type) is not required_quote_type
        ):
            summary = f"provider returned {quote.quote_type}; required {required_quote_type}"
            self._session.add(
                AuditEvent(
                    event_type="market_price_refresh_failed",
                    entity_type="asset",
                    entity_id=asset.id,
                    details_json=json.dumps(
                        {
                            "source": quote.source,
                            "source_url": quote.source_url,
                            "fetched_at": quote.fetched_at.isoformat(),
                            "attempts": quote.attempts,
                            "error_code": "unexpected_quote_type",
                            "error_text": summary,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
            )
            self._session.flush()
            return RefreshResult(
                status=RefreshStatus.FAILED,
                is_fresh=False,
                snapshot=None,
                last_good_snapshot=last_good,
                last_good_price=last_good.price if last_good is not None else None,
                source=quote.source,
                source_url=quote.source_url,
                fetched_at=quote.fetched_at,
                attempts=quote.attempts,
                error_summary=summary,
            )

        reference = self._clock()
        if (
            quote.fetched_at > reference
            or quote.valuation_date > reference.astimezone(SHANGHAI).date()
            or quote.valuation_date > quote.fetched_at.astimezone(SHANGHAI).date()
        ):
            summary = "invalid_quote_time: provider returned impossible timestamps"
            self._session.add(
                AuditEvent(
                    event_type="market_price_refresh_failed",
                    entity_type="asset",
                    entity_id=asset.id,
                    details_json=json.dumps(
                        {
                            "source": quote.source,
                            "source_url": quote.source_url,
                            "fetched_at": quote.fetched_at.isoformat(),
                            "attempts": quote.attempts,
                            "error_code": "invalid_quote_time",
                            "error_text": summary,
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
            )
            self._session.flush()
            return RefreshResult(
                status=RefreshStatus.FAILED,
                is_fresh=False,
                snapshot=None,
                last_good_snapshot=last_good,
                last_good_price=last_good.price if last_good is not None else None,
                source=quote.source,
                source_url=quote.source_url,
                fetched_at=quote.fetched_at,
                attempts=quote.attempts,
                error_summary=summary,
            )
        fresh = nav_is_fresh(quote.valuation_date, quote.fetched_at, reference)
        snapshot = self._session.scalar(
            select(PriceSnapshot).where(
                PriceSnapshot.asset_id == asset.id,
                PriceSnapshot.valuation_date == quote.valuation_date,
                PriceSnapshot.source == quote.source,
                PriceSnapshot.quote_type == quote.quote_type,
            )
        )
        if snapshot is None:
            snapshot = PriceSnapshot(
                asset_id=asset.id,
                valuation_date=quote.valuation_date,
                source=quote.source,
                price=quote.value,
                quote_type=quote.quote_type,
            )
            self._session.add(snapshot)
        snapshot.price = quote.value
        snapshot.source_url = quote.source_url
        snapshot.fetched_at = quote.fetched_at
        snapshot.error_text = None
        snapshot.quote_type = quote.quote_type
        self._session.flush()
        if quote.quote_type == QuoteType.OFFICIAL_NAV:
            refresh_current_snapshot_after_price_update(
                self._session, now=reference
            )

        return RefreshResult(
            status=RefreshStatus.SUCCESS,
            is_fresh=fresh,
            snapshot=snapshot,
            last_good_snapshot=snapshot,
            last_good_price=snapshot.price,
            source=quote.source,
            source_url=quote.source_url,
            fetched_at=quote.fetched_at,
            attempts=quote.attempts,
            error_summary=None,
        )

    def refresh_with_fallback(
        self,
        fund_code: str,
        providers: list[FundNavProvider],
        *,
        required_quote_type: QuoteType = QuoteType.OFFICIAL_NAV,
    ) -> RefreshResult:
        if not providers:
            raise ValueError("at least one NAV provider is required")
        attempts: list[ProviderAttempt] = []
        last_result: RefreshResult | None = None
        for provider in providers:
            result = FundPriceService(
                self._session, provider, clock=self._clock
            ).refresh(fund_code, required_quote_type=required_quote_type)
            matches = (
                result.snapshot is not None
                and QuoteType(result.snapshot.quote_type) is required_quote_type
            )
            attempt_status = result.status if matches else RefreshStatus.FAILED
            attempt_error = result.error_summary
            if result.snapshot is not None and not matches:
                attempt_status = RefreshStatus.FAILED
                attempt_error = (
                    f"provider returned {result.snapshot.quote_type}; "
                    f"required {required_quote_type.value}"
                )
            attempts.append(
                ProviderAttempt(provider.source, attempt_status, attempt_error)
            )
            last_result = result
            if result.status is RefreshStatus.SUCCESS and matches:
                return replace(result, provider_attempts=tuple(attempts))
        reference = self._clock()
        asset = self._session.scalar(
            select(Asset).where(
                Asset.code == fund_code,
                Asset.market == "CN",
                Asset.asset_class == "fund",
            )
        )
        last_good = (
            self._last_good(asset.id, required_quote_type) if asset is not None else None
        )
        if last_result is None:
            last_result = RefreshResult(
                status=RefreshStatus.FAILED,
                is_fresh=False,
                snapshot=None,
                last_good_snapshot=last_good,
                last_good_price=last_good.price if last_good is not None else None,
                source=providers[-1].source,
                source_url=providers[-1].source_url,
                fetched_at=reference,
                attempts=len(attempts),
                error_summary=(
                    f"no usable {required_quote_type.value} from configured providers"
                ),
            )
        return replace(
            last_result,
            status=RefreshStatus.FAILED,
            is_fresh=False,
            snapshot=None,
            last_good_snapshot=last_good,
            last_good_price=last_good.price if last_good is not None else None,
            error_summary=(
                f"no usable {required_quote_type.value} from configured providers"
            ),
            provider_attempts=tuple(attempts),
        )

    def _last_good(
        self,
        asset_id: int,
        quote_type: QuoteType = QuoteType.OFFICIAL_NAV,
    ) -> PriceSnapshot | None:
        rows = self._session.scalars(
            select(PriceSnapshot).where(
                PriceSnapshot.asset_id == asset_id,
                PriceSnapshot.error_text.is_(None),
                PriceSnapshot.price > 0,
                PriceSnapshot.quote_type == quote_type.value,
            )
        )
        return max(rows, key=price_selection_key, default=None)
