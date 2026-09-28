from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.db import utc_now
from finance_app.ledger.models import Asset, AuditEvent
from finance_app.market.base import FundNavProvider, MarketDataError
from finance_app.portfolio.models import PriceSnapshot
from finance_app.portfolio.rules import nav_is_fresh


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

    def refresh(self, fund_code: str) -> RefreshResult:
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

        last_good = self._last_good(asset.id)
        try:
            quote = self._provider.fetch(fund_code)
        except MarketDataError as error:
            self._session.add(
                AuditEvent(
                    event_type="market_price_refresh_failed",
                    entity_type="asset",
                    entity_id=asset.id,
                    details_json=json.dumps(
                        {
                            "source": error.source,
                            "source_url": error.source_url,
                            "fetched_at": error.fetched_at.isoformat(),
                            "attempts": error.attempts,
                            "error_code": error.code,
                            "error_text": error.summary,
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
                source=error.source,
                source_url=error.source_url,
                fetched_at=error.fetched_at,
                attempts=error.attempts,
                error_summary=error.summary,
            )

        reference = self._clock()
        fresh = nav_is_fresh(quote.valuation_date, quote.fetched_at, reference)
        snapshot = self._session.scalar(
            select(PriceSnapshot).where(
                PriceSnapshot.asset_id == asset.id,
                PriceSnapshot.valuation_date == quote.valuation_date,
                PriceSnapshot.source == quote.source,
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

        return RefreshResult(
            status=RefreshStatus.SUCCESS if fresh else RefreshStatus.STALE,
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

    def _last_good(self, asset_id: int) -> PriceSnapshot | None:
        return self._session.scalar(
            select(PriceSnapshot)
            .where(
                PriceSnapshot.asset_id == asset_id,
                PriceSnapshot.error_text.is_(None),
                PriceSnapshot.price > 0,
            )
            .order_by(
                PriceSnapshot.valuation_date.desc(),
                PriceSnapshot.fetched_at.desc(),
                PriceSnapshot.id.desc(),
            )
            .limit(1)
        )
