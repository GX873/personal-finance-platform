from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from getpass import getpass
from typing import Any

from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from finance_app.auth.models import User
from finance_app.auth.service import hash_password
from finance_app.calendar import is_skipped_reminder_day, parse_holidays
from finance_app.config import Settings, get_settings
from finance_app.db import get_session_factory, utc_now
from finance_app.ledger.budget import BucketKind, cycle_allocation, salary_cycle
from finance_app.ledger.models import (
    Asset,
    AuditEvent,
    CashBucket,
    Holding,
    MonthlyBudget,
    PortfolioRole,
    RiskLevel,
    Transaction,
)
from finance_app.market.base import FundNavProvider
from finance_app.market.eastmoney import EastMoneyFundNavProvider
from finance_app.market.efinance_adapter import EfinanceAdapter
from finance_app.market.nav_job import OfficialNavSyncJob
from finance_app.market.service import FundPriceService, RefreshResult, RefreshStatus
from finance_app.notifications.base import DeliveryStatus, Notification
from finance_app.notifications.email import EmailNotifier
from finance_app.notifications.models import (
    AppSetting,
    JobRun,
    NotificationChannel,
    NotificationDelivery,
)
from finance_app.notifications.service import NotificationDeliveryService
from finance_app.notifications.wechat import build_wechat_notifier
from finance_app.ops.backup import create_backup, restore_check
from finance_app.ops.export import ExportService
from finance_app.portfolio.models import (
    Alert,
    AllocationTarget,
    OpportunityAlert,
    PortfolioSnapshot,
    PriceSnapshot,
)
from finance_app.portfolio.opportunities import (
    OpportunityAction,
    OpportunityCandidate,
    evaluate_opportunity,
    opportunity_already_emitted,
)
from finance_app.portfolio.rules import (
    SHANGHAI,
    Advice,
    AdviceAction,
    RuleContext,
    evaluate_allocation,
    evaluate_new_investment,
    evaluate_reserve_shortfall,
    nav_is_fresh,
)
from finance_app.portfolio.service import create_daily_snapshot, price_selection_key

DAILY_JOB_NAME = "daily-check"
DEFAULT_SCHEDULE = "14:00"
STALE_JOB_AFTER = timedelta(hours=1)
_SCHEDULE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z", re.ASCII)


class DailyCheckResult(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    ALREADY_RAN = "already-ran"
    NOT_DUE = "not-due"
    DRY_RUN = "dry-run"


class LostJobClaim(RuntimeError):
    pass


class DailyCheck:
    """Coordinate one idempotent daily portfolio check."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        clock=utc_now,
        dry_run: bool = False,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.clock = clock
        self.dry_run = dry_run
        self._claim_id: int | None = None
        self._claim_started_at: datetime | None = None

    def run_scheduled(self) -> DailyCheckResult:
        now = self.clock()
        local = now.astimezone(SHANGHAI)
        holidays = parse_holidays(self._setting("cn_holidays", "[]"))
        if is_skipped_reminder_day(local.date(), holidays):
            return DailyCheckResult.NOT_DUE
        configured = self._setting("daily_schedule", DEFAULT_SCHEDULE)
        if _SCHEDULE.fullmatch(configured) is None:
            raise ValueError("stored daily schedule is invalid")
        hour, minute = (int(part) for part in configured.split(":"))
        scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidates = (scheduled, scheduled - timedelta(days=1))
        matched = next(
            (
                candidate
                for candidate in candidates
                if candidate <= local < candidate + timedelta(minutes=5)
            ),
            None,
        )
        if matched is None:
            return DailyCheckResult.NOT_DUE
        return self.run(matched.date())

    def run(self, business_date: date) -> DailyCheckResult:
        if type(business_date) is not date:
            raise ValueError("business_date must be a date")
        if self.dry_run:
            savepoint = self.session.begin_nested()
            try:
                freshness = self.validate_freshness(business_date)
                snapshot = self.create_snapshot(business_date, freshness)
                self.evaluate_rules(snapshot, freshness)
            finally:
                savepoint.rollback()
                self.session.expire_all()
            return DailyCheckResult.DRY_RUN

        job = JobRun(
            job_name=DAILY_JOB_NAME,
            business_date=business_date,
            status="running",
            started_at=self.clock(),
        )
        self.session.add(job)
        try:
            # Committing the unique claim makes concurrent workers observe it.
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(JobRun).where(
                    JobRun.job_name == DAILY_JOB_NAME,
                    JobRun.business_date == business_date,
                )
            )
            if existing is None or existing.status == "success":
                return DailyCheckResult.ALREADY_RAN
            stale_running = (
                existing.status == "running"
                and existing.started_at <= self.clock() - STALE_JOB_AFTER
            )
            if existing.status != "failed" and not stale_running:
                return DailyCheckResult.ALREADY_RAN
            claimed_id = self.session.scalar(
                update(JobRun)
                .where(
                    JobRun.id == existing.id,
                    JobRun.status == existing.status,
                    JobRun.started_at == existing.started_at,
                )
                .values(
                    status="running",
                    started_at=self.clock(),
                    completed_at=None,
                    error_summary=None,
                )
                .returning(JobRun.id)
            )
            if claimed_id is None:
                self.session.rollback()
                return DailyCheckResult.ALREADY_RAN
            self.session.commit()
            reclaimed_job = self.session.get(JobRun, claimed_id)
            assert reclaimed_job is not None
            job = reclaimed_job

        self._claim_id = job.id
        self._claim_started_at = job.started_at
        try:
            self.refresh_prices(business_date)
            freshness = self.validate_freshness(business_date)
            snapshot = self.create_snapshot(business_date, freshness)
            advice = self.evaluate_rules(snapshot, freshness)
            opportunities = self.scan_opportunities(snapshot, freshness, business_date)
            advice.extend(opportunities)
            if not self._renew_claim():
                self.session.rollback()
                return DailyCheckResult.ALREADY_RAN
            opportunity_status = DeliveryStatus.SUCCESS
            if opportunities:
                opportunity_status = self.notify(
                    Notification(
                        title=f"{business_date.isoformat()} 基金机会提醒",
                        body=self._opportunity_body(opportunities[0]),
                    ),
                    critical=True,
                )
            status = self.notify(
                self.build_digest(business_date, snapshot, freshness, advice)
            )
            if self._is_critical(advice):
                critical_status = self.notify(
                    Notification(
                        title=f"{business_date.isoformat()} 重大风险提醒",
                        body=self._critical_body(advice),
                    ),
                    critical=True,
                )
                if critical_status is DeliveryStatus.FAILED:
                    status = DeliveryStatus.FAILED
            if (
                status is DeliveryStatus.FAILED
                or opportunity_status is DeliveryStatus.FAILED
            ):
                return (
                    DailyCheckResult.FAILED
                    if self._finish_claim("failed", "notification_delivery_failed")
                    else DailyCheckResult.ALREADY_RAN
                )
            return (
                DailyCheckResult.SUCCESS
                if self._finish_claim("success", None)
                else DailyCheckResult.ALREADY_RAN
            )
        except LostJobClaim:
            self.session.rollback()
            return DailyCheckResult.ALREADY_RAN
        except Exception:  # noqa: BLE001
            self.session.rollback()
            return (
                DailyCheckResult.FAILED
                if self._finish_claim("failed", "daily_check_failed")
                else DailyCheckResult.ALREADY_RAN
            )

    def _renew_claim(self) -> bool:
        if self._claim_id is None or self._claim_started_at is None:
            return True
        renewed_at = self.clock()
        claimed_id = self.session.scalar(
            update(JobRun)
            .where(
                JobRun.id == self._claim_id,
                JobRun.status == "running",
                JobRun.started_at == self._claim_started_at,
            )
            .values(started_at=renewed_at)
            .returning(JobRun.id)
        )
        if claimed_id is None:
            self.session.rollback()
            return False
        self.session.commit()
        self._claim_started_at = renewed_at
        return True

    def _finish_claim(self, status: str, error_summary: str | None) -> bool:
        if self._claim_id is None or self._claim_started_at is None:
            return False
        claimed_id = self.session.scalar(
            update(JobRun)
            .where(
                JobRun.id == self._claim_id,
                JobRun.status == "running",
                JobRun.started_at == self._claim_started_at,
            )
            .values(
                status=status,
                completed_at=self.clock(),
                error_summary=error_summary,
            )
            .returning(JobRun.id)
        )
        if claimed_id is None:
            self.session.rollback()
            return False
        self.session.commit()
        return True

    def refresh_prices(self, business_date: date) -> list[Any]:
        results: list[Any] = []
        provider = EastMoneyFundNavProvider(clock=self.clock)
        history_provider = EfinanceAdapter(clock=self.clock)
        try:
            service = FundPriceService(self.session, provider, clock=self.clock)
            providers: list[FundNavProvider] = [provider, history_provider]
            fund_codes = self.session.scalars(
                select(Asset.code)
                .where(Asset.market == "CN", Asset.asset_class == "fund")
                .order_by(Asset.code)
            )
            for code in fund_codes:
                result = service.refresh_with_fallback(code, providers)
                results.append(result)
                if isinstance(result, RefreshResult) and result.status is RefreshStatus.FAILED:
                    self._record_manual_nav_recovery(code, business_date, result)
                self._backfill_history(code, business_date, history_provider)
            return results
        finally:
            provider.close()

    def _record_manual_nav_recovery(
        self, fund_code: str, business_date: date, result: RefreshResult
    ) -> None:
        asset = self.session.scalar(
            select(Asset).where(
                Asset.code == fund_code,
                Asset.market == "CN",
                Asset.asset_class == "fund",
            )
        )
        if asset is None:
            return
        asset_marker = f"（{asset.code}）"
        date_marker = f"业务日期：{business_date.isoformat()}；"
        existing = self.session.scalar(
            select(Alert).where(
                Alert.alert_type == "price",
                Alert.message.contains(asset_marker),
                Alert.message.contains(date_marker),
            )
        )
        if existing is not None:
            return
        last_date = (
            result.last_good_snapshot.valuation_date.isoformat()
            if result.last_good_snapshot is not None
            else "无"
        )
        sources = ", ".join(
            f"{item.source}:{'过期' if item.status is RefreshStatus.STALE else '失败'}"
            for item in result.provider_attempts
        ) or "自动来源失败"
        self.session.add(
            Alert(
                alert_type="price",
                severity="warning",
                message=(
                    f"需要手工补录基金净值：{asset.name}{asset_marker}。"
                    f"{date_marker}"
                    f"最后有效净值日期：{last_date}；{sources}。"
                    "未生成数值，请在净值页面核对后录入。"
                ),
            )
        )
        self.session.flush()

    def _backfill_history(
        self,
        fund_code: str,
        business_date: date,
        provider: EfinanceAdapter,
    ) -> int:
        asset = self.session.scalar(
            select(Asset).where(
                Asset.code == fund_code,
                Asset.market == "CN",
                Asset.asset_class == "fund",
            )
        )
        if asset is None:
            return 0
        count = self.session.scalar(
            select(func.count()).select_from(PriceSnapshot).where(
                PriceSnapshot.asset_id == asset.id,
                PriceSnapshot.quote_type == "official_nav",
                PriceSnapshot.error_text.is_(None),
            )
        )
        if count is not None and count >= 250:
            return 0
        try:
            quotes = provider.fetch_history(fund_code, limit=500)
        except Exception:  # noqa: BLE001 - optional provider failures degrade safely
            self.session.add(
                AuditEvent(
                    event_type="market_history_refresh_failed",
                    entity_type="asset",
                    entity_id=asset.id,
                    details_json=json.dumps(
                        {"source": provider.source, "error_code": "history_unavailable"},
                        separators=(",", ":"),
                    ),
                )
            )
            return 0
        inserted = 0
        for quote in quotes:
            if quote.valuation_date > business_date:
                continue
            existing = self.session.scalar(
                select(PriceSnapshot.id).where(
                    PriceSnapshot.asset_id == asset.id,
                    PriceSnapshot.valuation_date == quote.valuation_date,
                    PriceSnapshot.source == quote.source,
                    PriceSnapshot.quote_type == quote.quote_type,
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
                    quote_type=quote.quote_type,
                    source_url=quote.source_url,
                    fetched_at=quote.fetched_at,
                )
            )
            inserted += 1
        self.session.flush()
        return inserted

    def validate_freshness(self, business_date: date) -> dict[str, Any]:
        now = self._reference_time(business_date)
        holdings = list(
            self.session.scalars(select(Holding).where(Holding.quantity > 0))
        )
        price_rows: list[PriceSnapshot] = []
        prices_fresh = True
        for holding in holdings:
            eligible = list(
                self.session.scalars(
                select(PriceSnapshot)
                .where(
                    PriceSnapshot.asset_id == holding.asset_id,
                    PriceSnapshot.valuation_date <= business_date,
                    PriceSnapshot.fetched_at <= now,
                    PriceSnapshot.error_text.is_(None),
                    PriceSnapshot.quote_type == "official_nav",
                )
                )
            )
            price = max(eligible, key=price_selection_key, default=None)
            if price is None:
                prices_fresh = False
                continue
            valid_price = (
                price.price.is_finite() and price.price > 0 and bool(price.source.strip())
            )
            if valid_price:
                price_rows.append(price)
            if not valid_price:
                prices_fresh = False
        holdings_fresh = all(row.updated_at <= now for row in holdings)
        buckets = list(self.session.scalars(select(CashBucket)))
        required = {BucketKind.RESERVE.value, BucketKind.INVESTMENT.value}
        cash_fresh = required <= {row.bucket_kind for row in buckets} and all(
            row.updated_at <= now
            for row in buckets
            if row.bucket_kind in required
        )
        price_as_of = min((row.fetched_at for row in price_rows), default=None)
        holdings_as_of = min((row.updated_at for row in holdings), default=None)
        relevant_buckets = [row for row in buckets if row.bucket_kind in required]
        cash_as_of = min((row.updated_at for row in relevant_buckets), default=None)
        timestamps = [
            timestamp
            for timestamp in (price_as_of, holdings_as_of, cash_as_of)
            if timestamp is not None
        ]
        return {
            "prices_fresh": prices_fresh,
            "holdings_fresh": holdings_fresh,
            "cash_fresh": cash_fresh,
            "as_of": min(timestamps) if timestamps else now,
            "price_as_of": price_as_of,
            "holdings_as_of": holdings_as_of,
            "cash_as_of": cash_as_of,
            "sources": sorted({row.source for row in price_rows}),
        }

    def create_snapshot(
        self, business_date: date, freshness: dict[str, Any]
    ) -> PortfolioSnapshot:
        return create_daily_snapshot(
            self.session,
            now=self._reference_time(business_date),
            holdings_confirmed_at=(
                freshness.get("holdings_as_of") if freshness["holdings_fresh"] else None
            ),
            cash_confirmed_at=(
                freshness.get("cash_as_of") if freshness["cash_fresh"] else None
            ),
        )

    def _monthly_investment_remaining(self, business_date: date) -> int | None:
        cycle_start, cycle_end = salary_cycle(business_date)
        rows = list(
            self.session.scalars(
                select(MonthlyBudget).where(
                    MonthlyBudget.month == cycle_start.strftime("%Y-%m")
                )
            )
        )
        amounts = {row.bucket_kind: row.amount_cents for row in rows}
        expected = {kind.value for kind in BucketKind}
        if set(amounts) != expected or any(
            type(amount) is not int or not 0 <= amount <= 2**63 - 1
            for amount in amounts.values()
        ):
            return None
        month_start = datetime.combine(cycle_start, datetime.min.time(), SHANGHAI)
        next_month = datetime.combine(
            cycle_end + timedelta(days=1), datetime.min.time(), SHANGHAI
        )
        next_business_day = datetime.combine(
            business_date + timedelta(days=1), datetime.min.time(), SHANGHAI
        )
        period_end = min(next_month, next_business_day)
        reversed_ids = set(
            self.session.scalars(
                select(Transaction.reverses_transaction_id).where(
                    Transaction.reverses_transaction_id.is_not(None)
                )
            )
        )
        buys = self.session.scalars(
            select(Transaction).where(
                Transaction.kind == "BUY",
                Transaction.reverses_transaction_id.is_(None),
                Transaction.occurred_at >= month_start,
                Transaction.occurred_at < period_end,
            )
        )
        used = sum(
            row.amount_cents + row.fee_cents
            for row in buys
            if row.id not in reversed_ids
        )
        return max(0, amounts[BucketKind.INVESTMENT.value] - used)

    def _rule_context(
        self, freshness: dict[str, Any], business_date: date | None = None
    ) -> RuleContext:
        business_date = business_date or self.clock().astimezone(SHANGHAI).date()
        buckets = {
            row.bucket_kind: row.balance_cents
            for row in self.session.scalars(select(CashBucket))
        }
        reserve_target = int(self._setting("reserve_target_cents", "100000"))
        reserve_cents = buckets.get(BucketKind.RESERVE.value)
        cycle_start, _ = salary_cycle(business_date)
        special_used = (
            self.session.scalar(
                select(OpportunityAlert.id).where(
                    OpportunityAlert.cycle_start == cycle_start,
                    OpportunityAlert.used_special_budget.is_(True),
                )
            )
            is not None
        )
        allocation = (
            cycle_allocation(
                reserve_cents=reserve_cents,
                reserve_target_cents=reserve_target,
                normal_budget_cents=20000,
                special_used=special_used,
            )
            if reserve_cents is not None
            else None
        )
        return RuleContext(
            reserve_cents=reserve_cents,
            investment_cash_cents=buckets.get(BucketKind.INVESTMENT.value),
            prices_fresh=freshness["prices_fresh"],
            holdings_fresh=freshness["holdings_fresh"],
            cash_fresh=freshness["cash_fresh"],
            required_reserve_cents=reserve_target,
            month_budget_remaining_cents=self._monthly_investment_remaining(
                business_date
            ),
            source=",".join(freshness["sources"]) or "confirmed-ledger",
            timestamp=freshness["as_of"],
            special_opportunity_used=special_used,
            special_opportunity_cents=(
                allocation.special_opportunity_cents if allocation else 0
            ),
            reserve_replenishment_cents=(
                allocation.reserve_replenishment_cents if allocation else 0
            ),
        )

    def default_advice(
        self, freshness: dict[str, Any], business_date: date | None = None
    ) -> list[Advice]:
        context = self._rule_context(freshness, business_date)
        return [evaluate_new_investment(context, RiskLevel.HIGH, now=self.clock())]

    def evaluate_rules(
        self, snapshot: PortfolioSnapshot | None, freshness: dict[str, Any]
    ) -> list[Advice]:
        complete = snapshot is not None and snapshot.data_complete
        effective_freshness = dict(freshness)
        if not complete:
            effective_freshness.update(prices_fresh=False, holdings_fresh=False)
        business_date = (
            snapshot.snapshot_date
            if snapshot is not None
            else self.clock().astimezone(SHANGHAI).date()
        )
        advice = self.default_advice(effective_freshness, business_date)
        reserve_advice = evaluate_reserve_shortfall(
            self._rule_context(freshness, business_date), now=self.clock()
        )
        if reserve_advice is not None and not any(
            item.reason_code == "RESERVE_SHORTFALL" for item in advice
        ):
            advice.append(reserve_advice)
        if (
            snapshot is None
            or not snapshot.data_complete
            or snapshot.invested_value_cents <= 0
            or not snapshot.details_json
        ):
            return advice
        try:
            holdings = json.loads(snapshot.details_json).get("holdings", [])
        except (AttributeError, TypeError, ValueError):
            return advice
        if not isinstance(holdings, list):
            return advice
        targets = {
            row.name: row
            for row in self.session.scalars(select(AllocationTarget)).all()
        }
        context = self._rule_context(effective_freshness, business_date)
        role_values = {PortfolioRole.CORE.value: 0, PortfolioRole.SATELLITE.value: 0}
        satellite_positions: dict[int, tuple[Asset, int]] = {}
        for item in holdings:
            if not isinstance(item, dict) or type(item.get("value_cents")) is not int:
                continue
            asset_id = item.get("asset_id")
            asset = self.session.get(Asset, asset_id) if type(asset_id) is int else None
            if asset is None or asset.portfolio_role not in role_values:
                continue
            value = item["value_cents"]
            if value < 0:
                continue
            role_values[asset.portfolio_role] += value
            if asset.portfolio_role == PortfolioRole.SATELLITE:
                previous = satellite_positions.get(asset.id)
                satellite_positions[asset.id] = (
                    asset,
                    (previous[1] if previous is not None else 0) + value,
                )
        for role, value in role_values.items():
            target = targets.get(role)
            if target is None or value == 0:
                continue
            current_bps = value * 10000 // snapshot.invested_value_cents
            advice.append(
                evaluate_allocation(
                    context,
                    current_bps,
                    value,
                    target.target_bps,
                    target.target_bps,
                    invested_value_cents=snapshot.invested_value_cents,
                    now=self.clock(),
                    name=role,
                    scope="portfolio_role",
                )
            )
        each_target = targets.get("satellite_each")
        if each_target is not None and each_target.upper_bps is not None:
            for asset, value in satellite_positions.values():
                current_bps = value * 10000 // snapshot.invested_value_cents
                advice.append(
                    evaluate_allocation(
                        context,
                        current_bps,
                        value,
                        each_target.target_bps,
                        each_target.upper_bps,
                        invested_value_cents=snapshot.invested_value_cents,
                        now=self.clock(),
                        asset_id=asset.id,
                        code=asset.code,
                        name=asset.name,
                        scope="asset",
                    )
                )
        return advice

    def scan_opportunities(
        self,
        snapshot: PortfolioSnapshot | None,
        freshness: dict[str, Any],
        business_date: date,
        position_provider: EfinanceAdapter | None = None,
    ) -> list[Advice]:
        """Screen tracked, unheld funds; never invent missing overlap or history."""
        if (
            snapshot is None
            or not snapshot.data_complete
            or not all(
                freshness.get(key) is True
                for key in ("prices_fresh", "holdings_fresh", "cash_fresh")
            )
        ):
            return []
        context = self._rule_context(freshness, business_date)
        available = min(
            context.investment_cash_cents or 0,
            context.month_budget_remaining_cents or 0,
        )
        if available <= 0 and not context.special_opportunity_used:
            available = context.special_opportunity_cents
        if available <= 0:
            return []
        held_ids = set(
            self.session.scalars(select(Holding.asset_id).where(Holding.quantity > 0))
        )
        held_funds = list(
            self.session.scalars(
                select(Asset).where(
                    Asset.id.in_(held_ids),
                    Asset.market == "CN",
                    Asset.asset_class == "fund",
                )
            )
        )
        position_provider = position_provider or EfinanceAdapter()
        targets = {
            row.name: row for row in self.session.scalars(select(AllocationTarget))
        }
        role_values = {PortfolioRole.CORE.value: 0, PortfolioRole.SATELLITE.value: 0}
        try:
            details = json.loads(snapshot.details_json or "{}")
            for item in details.get("holdings", []):
                asset = self.session.get(Asset, item.get("asset_id"))
                if asset is not None and asset.portfolio_role in role_values:
                    role_values[asset.portfolio_role] += max(0, int(item["value_cents"]))
        except (AttributeError, KeyError, TypeError, ValueError):
            return []
        cycle_start, _ = salary_cycle(business_date)
        found: list[Advice] = []
        assets = self.session.scalars(
            select(Asset).where(
                Asset.market == "CN",
                Asset.asset_class == "fund",
                Asset.id.not_in(held_ids),
            )
        )
        for asset in assets:
            if opportunity_already_emitted(
                self.session, code=asset.code, cycle_start=cycle_start
            ):
                continue
            history = list(
                self.session.scalars(
                    select(PriceSnapshot)
                    .where(
                        PriceSnapshot.asset_id == asset.id,
                        PriceSnapshot.quote_type == "official_nav",
                        PriceSnapshot.error_text.is_(None),
                        PriceSnapshot.valuation_date <= business_date,
                    )
                    .order_by(PriceSnapshot.valuation_date)
                )
            )
            preferred_by_date: dict[date, PriceSnapshot] = {}
            for row in sorted(history, key=price_selection_key, reverse=True):
                preferred_by_date.setdefault(row.valuation_date, row)
            history = [preferred_by_date[day] for day in sorted(preferred_by_date)]
            if not history:
                continue
            prices = [row.price for row in history]
            latest = prices[-1]
            role_target = targets.get(asset.portfolio_role)
            target_bps = role_target.target_bps if role_target is not None else 0
            current_bps = (
                role_values[asset.portfolio_role] * 10000 // snapshot.invested_value_cents
                if snapshot.invested_value_cents > 0
                else 0
            )
            rank = sum(value <= latest for value in prices)
            valuation_percentile = rank * 100 // len(prices)
            base = prices[max(0, len(prices) - 21)]
            recent_gain_bps = int((latest / base - 1) * 10000) if base else 0
            peak = max(prices)
            drawdown_bps = int((peak - latest) / peak * 10000) if peak else 0
            overlap_bps = 0
            if held_funds:
                try:
                    candidate_positions = position_provider.fetch_positions(asset.code)
                    held_positions = [
                        position_provider.fetch_positions(row.code) for row in held_funds
                    ]
                except Exception:  # noqa: BLE001 - optional provider is a hard boundary
                    candidate_positions = {}
                    held_positions = []
                    self.session.add(
                        AuditEvent(
                            event_type="fund_overlap_refresh_failed",
                            entity_type="asset",
                            entity_id=asset.id,
                            details_json=json.dumps(
                                {
                                    "source": position_provider.source,
                                    "error_code": "positions_unavailable",
                                },
                                separators=(",", ":"),
                            ),
                        )
                    )
                if not candidate_positions or any(not item for item in held_positions):
                    continue
                overlap_bps = max(
                    sum(
                        min(weight, positions.get(code, 0))
                        for code, weight in candidate_positions.items()
                    )
                    for positions in held_positions
                )
            decision = evaluate_opportunity(
                OpportunityCandidate(
                    code=asset.code,
                    name=asset.name,
                    allocation_gap_bps=max(0, target_bps - current_bps),
                    overlap_bps=overlap_bps,
                    valuation_percentile=valuation_percentile,
                    recent_gain_bps=recent_gain_bps,
                    drawdown_bps=drawdown_bps,
                    history_days=len(history),
                    data_fresh=nav_is_fresh(
                        history[-1].valuation_date,
                        history[-1].fetched_at,
                        self.clock(),
                        holidays=parse_holidays(self._setting("cn_holidays", "[]")),
                    ),
                    available_cents=available,
                )
            )
            if decision.action is not OpportunityAction.BUY_IN_BATCHES:
                continue
            used_special = (context.investment_cash_cents or 0) < decision.amount_cents
            self.session.add(
                OpportunityAlert(
                    code=asset.code,
                    cycle_start=cycle_start,
                    action=decision.action.value,
                    amount_cents=decision.amount_cents,
                    reason_code=decision.reason_code,
                    data_as_of=history[-1].fetched_at,
                    used_special_budget=used_special,
                )
            )
            self.session.add(
                Alert(
                    alert_type="fund_opportunity",
                    severity="notice",
                    message=(
                        f"{asset.name}（{asset.code}）符合分批买入条件，"
                        f"建议上限 {self._yuan(decision.amount_cents)}；请人工确认。"
                    ),
                )
            )
            found.append(
                Advice(
                    action=AdviceAction.BUY_IN_BATCHES,
                    amount_cents=decision.amount_cents,
                    reason_code=decision.reason_code,
                    trigger="组合存在配置缺口，历史估值未处于高位，且近期没有追高信号。",
                    max_risk="基金仍可能继续下跌，投入本金可能发生损失。",
                    stop_discipline="分批执行；关键数据缺失或估值升至高位时停止买入。",
                    source=history[-1].source,
                    timestamp=history[-1].fetched_at,
                    asset_id=asset.id,
                    code=asset.code,
                    name=asset.name,
                    scope="opportunity",
                )
            )
            break
        return found

    def _opportunity_body(self, advice: Advice) -> str:
        amount = (
            "未知" if advice.amount_cents is None else self._yuan(advice.amount_cents)
        )
        identity = advice.name or advice.code or "未命名基金"
        if advice.code:
            identity = f"{identity}（{advice.code}）"
        return "\n".join(
            (
                f"对象：{identity}",
                f"行动：{advice.action.value}",
                f"金额上限：{amount}",
                f"触发条件：{advice.trigger}",
                f"最大风险：{advice.max_risk}",
                f"执行纪律：{advice.stop_discipline}",
                f"数据来源：{advice.source}",
                f"数据时间：{advice.timestamp.astimezone(SHANGHAI).isoformat(timespec='minutes')}",
                "条件式建议：不构成涨跌预测，不保证收益，不自动下单。",
            )
        )

    def build_digest(
        self,
        business_date: date,
        snapshot: PortfolioSnapshot | None,
        freshness: dict[str, Any],
        advice: list[Advice],
    ) -> Notification:
        summary_freshness = dict(freshness)
        total = (
            "未知"
            if snapshot is None or snapshot.total_value_cents is None
            else self._yuan(snapshot.total_value_cents)
        )
        buckets = list(
            self.session.scalars(select(CashBucket).order_by(CashBucket.bucket_kind))
        )
        bucket_labels = {"reserve": "生活备用金", "investment": "可投资现金"}
        bucket_text = (
            "、".join(
                f"{bucket_labels.get(row.bucket_kind, '其他现金')}="
                f"{self._yuan(row.balance_cents)}"
                for row in buckets
            )
            or "未知"
        )
        source_text = "、".join(summary_freshness["sources"]) or "未知"
        domain_times = "；".join(
            f"{label}="
            + (
                timestamp.astimezone(SHANGHAI).isoformat(timespec="minutes")
                if timestamp is not None
                else "未知"
            )
            for label, timestamp in (
                ("价格", summary_freshness.get("price_as_of")),
                ("持仓", summary_freshness.get("holdings_as_of")),
                ("现金", summary_freshness.get("cash_as_of")),
            )
        )
        if not any(
            summary_freshness.get(key)
            for key in ("price_as_of", "holdings_as_of", "cash_as_of")
        ):
            domain_times = summary_freshness["as_of"].astimezone(SHANGHAI).isoformat(
                timespec="minutes"
            )
        ratio_text = self._allocation_ratio_text(snapshot)
        lines = [
            f"总资产：{total}",
            f"现金桶：{bucket_text}",
            f"数据时间：{domain_times}",
            f"数据来源：{source_text}",
        ]
        if any(
            item.action is AdviceAction.REDUCE_IN_BATCHES
            and item.scope == "portfolio_role"
            and item.name == PortfolioRole.SATELLITE.value
            for item in advice
        ) and any(
            item.action is AdviceAction.REDUCE_IN_BATCHES and item.scope == "asset"
            for item in advice
        ):
            lines.append("注意：整体与单基金减持金额不可累加；单基金优先采用具体建议。")
        for item in advice:
            amount = (
                "未知" if item.amount_cents is None else self._yuan(item.amount_cents)
            )
            lines.extend((f"行动：{item.action.value}", f"金额：{amount}"))
            if item.scope is not None:
                identity = item.name or item.code or "未命名"
                if item.code is not None:
                    identity = f"{identity}（{item.code}）"
                elif item.scope == "portfolio_role":
                    identity = f"{identity}（整体）"
                lines.append(f"对象：{identity}")
            if item.current_bps is not None and item.upper_bps is not None:
                lines.append(
                    "当前占比："
                    f"{Decimal(item.current_bps) / 100:.2f}%；"
                    f"上限：{Decimal(item.upper_bps) / 100:.2f}%"
                )
            else:
                lines.append(f"比例：{ratio_text}")
            lines.extend(
                (
                    f"触发条件：{item.trigger}",
                    f"最大风险：{item.max_risk}",
                    f"止损纪律：{item.stop_discipline}",
                    "条件式建议：仅供人工复核，不自动交易。",
                )
            )
        return Notification(
            title=f"{business_date.isoformat()} 每日理财摘要",
            body="\n".join(lines),
        )

    def _allocation_ratio_text(self, snapshot: PortfolioSnapshot | None) -> str:
        if (
            snapshot is None
            or snapshot.invested_value_cents is None
            or snapshot.invested_value_cents <= 0
            or not snapshot.details_json
        ):
            return "未知"
        try:
            holdings = json.loads(snapshot.details_json).get("holdings", [])
        except (AttributeError, TypeError, ValueError):
            return "未知"
        role_values = {PortfolioRole.CORE.value: 0, PortfolioRole.SATELLITE.value: 0}
        for item in holdings if isinstance(holdings, list) else []:
            if not isinstance(item, dict) or type(item.get("value_cents")) is not int:
                continue
            asset_id = item.get("asset_id")
            asset = self.session.get(Asset, asset_id) if type(asset_id) is int else None
            if asset is not None and asset.portfolio_role in role_values:
                role_values[asset.portfolio_role] += max(item["value_cents"], 0)
        return "、".join(
            f"{role}={Decimal(value * 10000 // snapshot.invested_value_cents) / 100:.2f}%"
            for role, value in role_values.items()
        )

    def notify(
        self, notification: Notification, *, critical: bool = False
    ) -> DeliveryStatus:
        del critical
        channels = list(
            self.session.scalars(
                select(NotificationChannel)
                .where(NotificationChannel.enabled.is_(True))
                .order_by(NotificationChannel.id)
            )
        )
        if not channels:
            return DeliveryStatus.SUCCESS
        service = NotificationDeliveryService(self.session)
        failed = False
        adapters = []
        for channel in channels:
            notifier = (
                EmailNotifier(self.settings)
                if channel.channel_type == "email"
                else build_wechat_notifier(channel.channel_type, self.settings)
            )
            adapters.append((channel, notifier))
        for channel, notifier in adapters:
            delivered = self.session.scalar(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.channel_id == channel.id,
                    NotificationDelivery.title == notification.title,
                    NotificationDelivery.status == DeliveryStatus.SUCCESS.value,
                )
            )
            if delivered is not None:
                continue
            if not self._renew_claim():
                raise LostJobClaim
            result = service.deliver(channel, notifier, notification)
            # Persist each external outcome before attempting the next channel.
            self.session.commit()
            failed = failed or result.status is DeliveryStatus.FAILED
        return DeliveryStatus.FAILED if failed else DeliveryStatus.SUCCESS

    def _setting(self, key: str, default: str) -> str:
        value = self.session.scalar(
            select(AppSetting.value).where(AppSetting.key == key)
        )
        return value if value is not None else default

    def _reference_time(self, business_date: date) -> datetime:
        current = self.clock()
        local = current.astimezone(SHANGHAI)
        if local.date() == business_date:
            return current
        return local.replace(
            year=business_date.year,
            month=business_date.month,
            day=business_date.day,
        ).astimezone(current.tzinfo)

    @staticmethod
    def _is_critical(advice: list[Advice]) -> bool:
        return any(item.reason_code == "RESERVE_SHORTFALL" for item in advice)

    @staticmethod
    def _critical_body(advice: list[Advice]) -> str:
        item = next(item for item in advice if item.reason_code == "RESERVE_SHORTFALL")
        return "\n".join(
            (
                f"行动：{item.action.value}",
                f"触发条件：{item.trigger}",
                f"最大风险：{item.max_risk}",
                f"止损纪律：{item.stop_discipline}",
            )
        )

    @staticmethod
    def _yuan(cents: int) -> str:
        return f"{Decimal(cents) / 100:,.2f} 元"


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "date must be a valid ISO date (YYYY-MM-DD)"
        ) from exc


def _password_command(args: argparse.Namespace) -> int:
    if not args.username.strip() or len(args.username) > 128:
        print("Username must contain 1 to 128 characters.", file=sys.stderr)
        return 1
    with get_session_factory()() as db:
        if not inspect(db.get_bind()).has_table("users"):
            print(
                "Database is not initialized. Run: python -m alembic upgrade head",
                file=sys.stderr,
            )
            return 1
        user = db.scalar(select(User).where(User.username == args.username))
        if args.command == "init-admin":
            if (
                db.scalar(select(User.id).where(User.is_active.is_(True))) is not None
                or user is not None
            ):
                print(
                    "An active administrator or this username already exists.",
                    file=sys.stderr,
                )
                return 1
        elif user is None or not user.is_active:
            print("Active user not found.", file=sys.stderr)
            return 1
        try:
            password = getpass("Password: ")
            confirmation = getpass("Repeat password: ")
            if password != confirmation:
                raise ValueError("Passwords do not match.")
            password_hash = hash_password(password)
        except (ValueError, EOFError, KeyboardInterrupt) as exc:
            print(str(exc) or "Password entry cancelled.", file=sys.stderr)
            return 1
        if args.command == "init-admin":
            db.add(User(username=args.username, password_hash=password_hash))
        else:
            assert user is not None
            user.password_hash = password_hash
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            print(
                "User could not be saved; the username may already exist.",
                file=sys.stderr,
            )
            return 1
    print(
        "Administrator created."
        if args.command == "init-admin"
        else "Password changed."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finance")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init-admin", "change-password"):
        command = commands.add_parser(name)
        command.add_argument("--username", required=True)
    daily = commands.add_parser("daily-check")
    mode = daily.add_mutually_exclusive_group(required=True)
    mode.add_argument("--date", type=_iso_date)
    mode.add_argument("--scheduled", action="store_true")
    daily.add_argument("--dry-run", action="store_true")
    nav = commands.add_parser("nav-refresh")
    nav.add_argument("--scheduled", action="store_true", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--directory", default=None)
    backup.add_argument("--keep", type=int, default=14)
    restore = commands.add_parser("restore-check")
    restore.add_argument("path")
    export = commands.add_parser("export")
    export.add_argument("--directory", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    if args.command == "backup":
        try:
            with get_session_factory()() as db:
                backup_path = create_backup(db, args.directory, keep=args.keep, session=db)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Backup failed: {exc}", file=sys.stderr)
            return 1
        print(backup_path)
        return 0
    if args.command == "restore-check":
        try:
            with get_session_factory()() as db:
                restore_result = restore_check(args.path, session=db)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Restore check failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"{restore_result.path} integrity={restore_result.integrity_check} "
            f"sha256={restore_result.checksum}"
        )
        return 0
    if args.command == "export":
        with get_session_factory()() as db:
            export_paths = ExportService(db).export_all(args.directory)
        for path in export_paths.values():
            print(path)
        return 0
    if args.command == "nav-refresh":
        primary = None
        try:
            with get_session_factory()() as db:
                primary = EastMoneyFundNavProvider()
                try:
                    result = OfficialNavSyncJob(
                        db, providers=[primary, EfinanceAdapter()]
                    ).run_scheduled()
                finally:
                    primary.close()
        except Exception:  # noqa: BLE001 - sanitize the CLI boundary
            print(
                "nav-refresh status=failed error=internal_error",
                file=sys.stderr,
            )
            return 1
        print(
            f"nav-refresh status={result.status} attempted={result.attempted} "
            f"succeeded={result.succeeded} unchanged={result.unchanged} "
            f"failed={result.failed}"
        )
        return 1 if result.status == "failed" else 0
    if args.command != "daily-check":
        return _password_command(args)
    with get_session_factory()() as db:
        if not inspect(db.get_bind()).has_table("job_runs"):
            print(
                "Database is not initialized. Run: python -m alembic upgrade head",
                file=sys.stderr,
            )
            return 1
        check = DailyCheck(db, dry_run=args.dry_run)
        if args.scheduled:
            daily_result = check.run_scheduled()
        else:
            assert isinstance(args.date, date)
            daily_result = check.run(args.date)
    print(daily_result.value)
    return 1 if daily_result is DailyCheckResult.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
