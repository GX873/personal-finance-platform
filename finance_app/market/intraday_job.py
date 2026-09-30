"""Scheduled refresh orchestration for held-fund intraday estimates."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.calendar import is_skipped_reminder_day, parse_holidays
from finance_app.db import utc_now
from finance_app.ledger.models import Asset, Holding
from finance_app.market.base import FundNavProvider, QuoteType
from finance_app.market.service import FundPriceService, RefreshStatus
from finance_app.notifications.models import AppSetting
from finance_app.portfolio.rules import SHANGHAI, aware

MORNING = (time(9, 30), time(11, 30))
AFTERNOON = (time(13, 0), time(15, 0))


def is_intraday_refresh_time(now: datetime, holidays: frozenset[date]) -> bool:
    """Return whether *now* falls in an inclusive Shanghai trading session."""
    aware(now)
    local = now.astimezone(SHANGHAI)
    if is_skipped_reminder_day(local.date(), holidays):
        return False
    current = local.time().replace(second=0, microsecond=0, tzinfo=None)
    return (
        MORNING[0] <= current <= MORNING[1] or AFTERNOON[0] <= current <= AFTERNOON[1]
    )


@dataclass(frozen=True)
class IntradayJobResult:
    status: str
    attempted: int
    succeeded: int
    failed: int


class IntradayRefreshJob:
    """Refresh intraday estimates for distinct, positively held CN funds."""

    def __init__(
        self,
        session: Session,
        *,
        providers: Sequence[FundNavProvider],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not providers:
            raise ValueError("at least one estimate provider is required")
        self.session = session
        self.providers = list(providers)
        self.clock = clock

    def run_scheduled(self) -> IntradayJobResult:
        holidays = parse_holidays(self._setting("cn_holidays", "[]"))
        if not is_intraday_refresh_time(self.clock(), holidays):
            return IntradayJobResult("skipped", 0, 0, 0)
        return self.refresh_held_funds()

    def refresh_held_funds(self) -> IntradayJobResult:
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
        failed = 0
        service = FundPriceService(self.session, self.providers[0], clock=self.clock)
        for code in codes:
            result = service.refresh_with_fallback(
                code,
                self.providers,
                required_quote_type=QuoteType.INTRADAY_ESTIMATE,
            )
            if result.status is RefreshStatus.SUCCESS:
                succeeded += 1
            else:
                failed += 1
        self.session.commit()
        status = "failed" if codes and failed == len(codes) else "success"
        return IntradayJobResult(status, len(codes), succeeded, failed)

    def _setting(self, key: str, default: str) -> str:
        value = self.session.scalar(
            select(AppSetting.value).where(AppSetting.key == key)
        )
        return value if value is not None else default
