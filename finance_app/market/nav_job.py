"""Scheduled synchronization of official NAVs for held mainland funds."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.calendar import is_skipped_reminder_day, parse_holidays
from finance_app.db import utc_now
from finance_app.ledger.models import Asset, AuditEvent, Holding
from finance_app.market.base import FundNavProvider, FundNavQuote, QuoteType
from finance_app.market.service import FundPriceService, RefreshStatus
from finance_app.notifications.models import AppSetting
from finance_app.portfolio.models import PriceSnapshot
from finance_app.portfolio.rules import SHANGHAI


@dataclass(frozen=True)
class NavSyncResult:
    status: str
    attempted: int
    succeeded: int
    unchanged: int
    failed: int


class FundNavHistoryProvider(Protocol):
    source: str

    def fetch_history(
        self, fund_code: str, *, limit: int = 500
    ) -> list[FundNavQuote]: ...


class OfficialNavSyncJob:
    """Refresh held NAVs and independently backfill tracked fund history."""

    def __init__(
        self,
        session: Session,
        *,
        providers: Sequence[FundNavProvider],
        history_provider: FundNavHistoryProvider | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not providers:
            raise ValueError("at least one official NAV provider is required")
        self.session = session
        self.providers = list(providers)
        if history_provider is None:
            history_provider = next(
                (
                    cast(FundNavHistoryProvider, provider)
                    for provider in providers
                    if callable(getattr(provider, "fetch_history", None))
                ),
                None,
            )
        self.history_provider = history_provider
        self.clock = clock

    def run_scheduled(self) -> NavSyncResult:
        local = self.clock().astimezone(SHANGHAI)
        holidays = parse_holidays(self._setting("cn_holidays", "[]"))
        if local.hour >= 12 and is_skipped_reminder_day(local.date(), holidays):
            return NavSyncResult("skipped", 0, 0, 0, 0)
        result = self.refresh_held_funds()
        self.backfill_tracked_funds()
        return result

    def refresh_held_funds(self) -> NavSyncResult:
        codes = self.session.scalars(
            select(Asset.code)
            .join(Holding, Holding.asset_id == Asset.id)
            .where(
                Holding.quantity > 0,
                Asset.market == "CN",
                Asset.asset_class == "fund",
            )
            .distinct()
            .order_by(Asset.code)
        ).all()
        succeeded = 0
        unchanged = 0
        failed = 0
        service = FundPriceService(self.session, self.providers[0], clock=self.clock)
        for code in codes:
            existing_ids = set(
                self.session.scalars(
                    select(PriceSnapshot.id)
                    .join(Asset, PriceSnapshot.asset_id == Asset.id)
                    .where(
                        Asset.code == code,
                        Asset.market == "CN",
                        Asset.asset_class == "fund",
                        PriceSnapshot.quote_type == QuoteType.OFFICIAL_NAV,
                    )
                )
            )
            result = service.refresh_with_fallback(
                code,
                self.providers,
                required_quote_type=QuoteType.OFFICIAL_NAV,
            )
            if result.status is not RefreshStatus.SUCCESS or result.snapshot is None:
                failed += 1
            elif result.snapshot.id in existing_ids:
                unchanged += 1
            else:
                succeeded += 1
        self.session.commit()
        status = "failed" if codes and failed == len(codes) else "success"
        return NavSyncResult(status, len(codes), succeeded, unchanged, failed)

    def backfill_tracked_funds(self) -> int:
        if self.history_provider is None:
            return 0
        assets = list(
            self.session.scalars(
                select(Asset)
                .where(Asset.market == "CN", Asset.asset_class == "fund")
                .order_by(Asset.code)
            )
        )
        inserted = 0
        business_date = self.clock().astimezone(SHANGHAI).date()
        for asset in assets:
            try:
                quotes = self.history_provider.fetch_history(asset.code, limit=500)
            except Exception:  # noqa: BLE001 - one optional history failure is isolated
                self.session.add(
                    AuditEvent(
                        event_type="market_history_refresh_failed",
                        entity_type="asset",
                        entity_id=asset.id,
                        details_json=json.dumps(
                            {
                                "source": self.history_provider.source,
                                "error_code": "history_unavailable",
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
                continue
            for quote in quotes:
                if (
                    quote.quote_type != QuoteType.OFFICIAL_NAV
                    or quote.valuation_date > business_date
                ):
                    continue
                existing = self.session.scalar(
                    select(PriceSnapshot.id).where(
                        PriceSnapshot.asset_id == asset.id,
                        PriceSnapshot.valuation_date == quote.valuation_date,
                        PriceSnapshot.source == quote.source,
                        PriceSnapshot.quote_type == QuoteType.OFFICIAL_NAV,
                    )
                )
                if existing is not None:
                    continue
                self.session.add(
                    PriceSnapshot(
                        asset_id=asset.id,
                        valuation_date=quote.valuation_date,
                        source=quote.source,
                        price=quote.value,
                        quote_type=QuoteType.OFFICIAL_NAV,
                        source_url=quote.source_url,
                        fetched_at=quote.fetched_at,
                    )
                )
                inserted += 1
        self.session.commit()
        return inserted

    def _setting(self, key: str, default: str) -> str:
        value = self.session.scalar(
            select(AppSetting.value).where(AppSetting.key == key)
        )
        return value if value is not None else default
