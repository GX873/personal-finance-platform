"""Scheduled synchronization of official NAVs for held mainland funds."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.calendar import is_skipped_reminder_day, parse_holidays
from finance_app.db import utc_now
from finance_app.ledger.models import Asset, Holding
from finance_app.market.base import FundNavProvider, QuoteType
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


class OfficialNavSyncJob:
    """Refresh official NAVs for distinct, positively held mainland funds."""

    def __init__(
        self,
        session: Session,
        *,
        providers: Sequence[FundNavProvider],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not providers:
            raise ValueError("at least one official NAV provider is required")
        self.session = session
        self.providers = list(providers)
        self.clock = clock

    def run_scheduled(self) -> NavSyncResult:
        local = self.clock().astimezone(SHANGHAI)
        holidays = parse_holidays(self._setting("cn_holidays", "[]"))
        if local.hour >= 12 and is_skipped_reminder_day(local.date(), holidays):
            return NavSyncResult("skipped", 0, 0, 0, 0)
        return self.refresh_held_funds()

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

    def _setting(self, key: str, default: str) -> str:
        value = self.session.scalar(
            select(AppSetting.value).where(AppSetting.key == key)
        )
        return value if value is not None else default
