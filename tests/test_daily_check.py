from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from finance_app.cli import DailyCheck, DailyCheckResult, main
from finance_app.ledger.models import (
    Account,
    Asset,
    CashBucket,
    Holding,
    MonthlyBudget,
    PortfolioRole,
    Transaction,
)
from finance_app.market.service import ProviderAttempt, RefreshResult, RefreshStatus
from finance_app.notifications.base import DeliveryResult, DeliveryStatus, Notification
from finance_app.notifications.models import (
    AppSetting,
    JobRun,
    NotificationChannel,
    NotificationDelivery,
)
from finance_app.portfolio.models import (
    Alert,
    AllocationTarget,
    OpportunityAlert,
    PortfolioSnapshot,
    PriceSnapshot,
)
from finance_app.portfolio.opportunities import evaluate_opportunity
from finance_app.portfolio.rules import Advice, AdviceAction
from tests.test_database import db_session as database_session_fixture

db_session = database_session_fixture

NOW = datetime(2026, 9, 23, 1, 2, tzinfo=UTC)  # 09:02 in Beijing


class RecordingDailyCheck(DailyCheck):
    def __init__(self, *args, fail_notification: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.events: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.fail_notification = fail_notification

    def refresh_prices(self, business_date):
        self.events.append("refresh_prices")
        return []

    def validate_freshness(self, business_date):
        self.events.append("validate_freshness")
        return {
            "prices_fresh": False,
            "holdings_fresh": False,
            "cash_fresh": False,
            "as_of": self.clock(),
            "sources": [],
        }

    def create_snapshot(self, business_date, freshness):
        self.events.append("snapshot")

    def evaluate_rules(self, snapshot, freshness):
        self.events.append("evaluate_rules")
        return self.default_advice(freshness)

    def notify(self, notification, *, critical=False):
        self.events.append("notify")
        self.notifications.append((notification.title, notification.body))
        return (
            DeliveryStatus.FAILED if self.fail_notification else DeliveryStatus.SUCCESS
        )


class CriticalRecordingDailyCheck(RecordingDailyCheck):
    def evaluate_rules(self, snapshot, freshness):
        self.events.append("evaluate_rules")
        return [
            Advice(
                action=AdviceAction.WAIT,
                amount_cents=0,
                reason_code="RESERVE_SHORTFALL",
                trigger="备用金未达标，暂停新增投资。",
                max_risk="高波动投资可能损失全部本金。",
                stop_discipline="先补足备用金，并由人工复核。",
                source="confirmed-ledger",
                timestamp=NOW,
            )
        ]


class NoNetworkDailyCheck(DailyCheck):
    def refresh_prices(self, business_date):
        return []

    def notify(self, notification, *, critical=False):
        return DeliveryStatus.SUCCESS


class RealRuleRecordingDailyCheck(DailyCheck):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.advice: list[Advice] = []
        self.notifications: list[tuple[str, bool]] = []

    def refresh_prices(self, business_date):
        return []

    def evaluate_rules(self, snapshot, freshness):
        self.advice = super().evaluate_rules(snapshot, freshness)
        return self.advice

    def notify(self, notification, *, critical=False):
        self.notifications.append((notification.title, critical))
        return DeliveryStatus.SUCCESS


class ClaimStolenDuringNotify(RecordingDailyCheck):
    def notify(self, notification, *, critical=False):
        self.session.execute(
            update(JobRun)
            .where(JobRun.job_name == "daily-check")
            .values(started_at=NOW + timedelta(minutes=1))
        )
        self.session.commit()
        return DeliveryStatus.SUCCESS


class StubNotifier:
    def __init__(self, provider: str, result: DeliveryResult):
        self.provider = provider
        self.result = result
        self.calls = 0

    def send(self, notification):
        self.calls += 1
        return self.result


def test_refresh_prices_closes_owned_provider_after_success(db_session, monkeypatch):
    db_session.add(
        Asset(code="000001", market="CN", name="Fund", asset_class="fund")
    )
    db_session.flush()

    class Provider:
        closed = False

        def close(self):
            self.closed = True

    provider = Provider()

    class Service:
        def __init__(self, session, actual_provider, *, clock):
            assert session is db_session
            assert actual_provider is provider

        def refresh_with_fallback(self, code, providers):
            return code

    monkeypatch.setattr(
        "finance_app.cli.EastMoneyFundNavProvider", lambda *, clock: provider
    )
    monkeypatch.setattr("finance_app.cli.FundPriceService", Service)

    result = DailyCheck(db_session, clock=lambda: NOW).refresh_prices(
        date(2026, 9, 23)
    )

    assert result == ["000001"]
    assert provider.closed is True


def test_refresh_prices_closes_owned_provider_after_unexpected_error(
    db_session, monkeypatch
):
    db_session.add(
        Asset(code="000001", market="CN", name="Fund", asset_class="fund")
    )
    db_session.flush()

    class Provider:
        closed = False

        def close(self):
            self.closed = True

    provider = Provider()

    class Service:
        def __init__(self, session, actual_provider, *, clock):
            pass

        def refresh_with_fallback(self, code, providers):
            raise RuntimeError("unexpected refresh failure")

    monkeypatch.setattr(
        "finance_app.cli.EastMoneyFundNavProvider", lambda *, clock: provider
    )
    monkeypatch.setattr("finance_app.cli.FundPriceService", Service)

    with pytest.raises(RuntimeError, match="unexpected refresh failure"):
        DailyCheck(db_session, clock=lambda: NOW).refresh_prices(date(2026, 9, 23))

    assert provider.closed is True


def test_failed_official_sources_create_one_manual_recovery_alert(
    db_session, monkeypatch
):
    db_session.add(
        Asset(code="000001", market="CN", name="Fund", asset_class="fund")
    )
    db_session.flush()

    class Provider:
        closed = False
        source = "eastmoney"
        source_url = "https://example.test/eastmoney"

        def close(self):
            self.closed = True

    provider = Provider()
    failed = RefreshResult(
        status=RefreshStatus.FAILED,
        is_fresh=False,
        snapshot=None,
        last_good_snapshot=None,
        last_good_price=None,
        source="efinance",
        source_url="https://example.test/efinance",
        fetched_at=NOW,
        attempts=1,
        error_summary="all NAV providers failed",
        provider_attempts=(
            ProviderAttempt("eastmoney", RefreshStatus.FAILED, "timeout"),
            ProviderAttempt("efinance", RefreshStatus.FAILED, "unavailable"),
        ),
    )

    class Service:
        def __init__(self, session, actual_provider, *, clock):
            pass

        def refresh_with_fallback(self, code, providers):
            return failed

    monkeypatch.setattr("finance_app.cli.EastMoneyFundNavProvider", lambda *, clock: provider)
    monkeypatch.setattr("finance_app.cli.FundPriceService", Service)
    monkeypatch.setattr(DailyCheck, "_backfill_history", lambda *args: 0)

    check = DailyCheck(db_session, clock=lambda: NOW)
    check.refresh_prices(date(2026, 9, 23))
    check.refresh_prices(date(2026, 9, 23))

    alerts = list(db_session.scalars(select(Alert)))
    assert len(alerts) == 1
    assert "000001" in alerts[0].message
    assert "手工补录" in alerts[0].message
    assert "未生成数值" in alerts[0].message
    assert "1.234" not in alerts[0].message


def test_manual_recovery_alert_is_idempotent_per_asset_and_business_date(
    db_session, monkeypatch
):
    db_session.add(
        Asset(code="000001", market="CN", name="Fund", asset_class="fund")
    )
    db_session.flush()

    class Provider:
        source = "eastmoney"
        source_url = "https://example.test/eastmoney"

        def close(self):
            pass

    failed = RefreshResult(
        status=RefreshStatus.FAILED,
        is_fresh=False,
        snapshot=None,
        last_good_snapshot=None,
        last_good_price=None,
        source="efinance",
        source_url="https://example.test/efinance",
        fetched_at=NOW,
        attempts=2,
        error_summary="no fresh official NAV",
        provider_attempts=(
            ProviderAttempt("eastmoney", RefreshStatus.STALE, None),
            ProviderAttempt("efinance", RefreshStatus.FAILED, "unavailable"),
        ),
    )

    class Service:
        def __init__(self, session, actual_provider, *, clock):
            pass

        def refresh_with_fallback(self, code, providers):
            return failed

    monkeypatch.setattr("finance_app.cli.EastMoneyFundNavProvider", lambda *, clock: Provider())
    monkeypatch.setattr("finance_app.cli.FundPriceService", Service)
    monkeypatch.setattr(DailyCheck, "_backfill_history", lambda *args: 0)

    check = DailyCheck(db_session, clock=lambda: NOW)
    check.refresh_prices(date(2026, 9, 22))
    check.refresh_prices(date(2026, 9, 22))
    check.refresh_prices(date(2026, 9, 23))

    alerts = list(db_session.scalars(select(Alert).order_by(Alert.id)))
    assert len(alerts) == 2
    assert "业务日期：2026-09-22" in alerts[0].message
    assert "业务日期：2026-09-23" in alerts[1].message
    assert [alert.alert_type for alert in alerts] == ["price", "price"]
    assert "eastmoney:过期" in alerts[0].message
    assert "efinance:失败" in alerts[0].message


def test_daily_check_orders_freshness_before_advice(db_session):
    check = RecordingDailyCheck(db_session, clock=lambda: NOW)

    result = check.run(date(2026, 9, 23))

    assert result is DailyCheckResult.SUCCESS
    assert check.events == [
        "refresh_prices",
        "validate_freshness",
        "snapshot",
        "evaluate_rules",
        "notify",
    ]


def test_daily_digest_combines_general_advice_and_discloses_unknown_data(db_session):
    check = RecordingDailyCheck(db_session, clock=lambda: NOW)

    check.run(date(2026, 9, 23))

    assert [title for title, _body in check.notifications] == [
        "2026-09-23 每日理财摘要"
    ]
    body = check.notifications[0][1]
    for required in (
        "总资产：未知",
        "现金桶",
        "数据时间",
        "数据来源：未知",
        "行动：WAIT_FOR_DATA",
        "金额：未知",
        "比例：未知",
        "触发条件",
        "最大风险",
        "止损纪律",
        "条件式建议",
    ):
        assert required in body


def test_critical_risk_can_send_one_separate_alert(db_session):
    check = CriticalRecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS

    assert [title for title, _body in check.notifications] == [
        "2026-09-23 每日理财摘要",
        "2026-09-23 重大风险提醒",
    ]


def test_duplicate_or_overlapping_daily_run_exits_cleanly(db_session):
    first = RecordingDailyCheck(db_session, clock=lambda: NOW)
    second = RecordingDailyCheck(db_session, clock=lambda: NOW)

    assert first.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS
    assert second.run(date(2026, 9, 23)) is DailyCheckResult.ALREADY_RAN
    assert second.events == []
    assert len(db_session.scalars(select(JobRun)).all()) == 1


def test_explicit_business_date_controls_snapshot_date(db_session):
    next_day = datetime(2026, 9, 24, 1, 2, tzinfo=UTC)
    check = NoNetworkDailyCheck(db_session, clock=lambda: next_day)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS

    assert db_session.scalar(select(PortfolioSnapshot)).snapshot_date == date(
        2026, 9, 23
    )


def test_notification_failure_marks_job_failed_and_returns_failure(db_session):
    check = RecordingDailyCheck(db_session, clock=lambda: NOW, fail_notification=True)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.FAILED

    row = db_session.scalar(select(JobRun))
    assert row is not None
    assert row.status == "failed"
    assert row.completed_at is not None
    assert row.error_summary == "notification_delivery_failed"

    repeated = RecordingDailyCheck(db_session, clock=lambda: NOW)
    assert repeated.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS
    assert repeated.events


def test_stale_running_job_is_atomically_recovered(db_session):
    db_session.add(
        JobRun(
            job_name="daily-check",
            business_date=date(2026, 9, 23),
            status="running",
            started_at=NOW - timedelta(hours=2),
        )
    )
    db_session.commit()
    check = RecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS

    row = db_session.scalar(select(JobRun))
    assert row.status == "success"
    assert row.started_at == NOW


def test_active_running_job_is_not_taken_over(db_session):
    db_session.add(
        JobRun(
            job_name="daily-check",
            business_date=date(2026, 9, 23),
            status="running",
            started_at=NOW - timedelta(minutes=30),
        )
    )
    db_session.commit()
    check = RecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.ALREADY_RAN
    assert check.events == []
    assert db_session.scalar(select(JobRun.status)) == "running"


def test_old_owner_cannot_overwrite_reclaimed_job(db_session):
    check = ClaimStolenDuringNotify(db_session, clock=lambda: NOW)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.ALREADY_RAN

    row = db_session.scalar(select(JobRun))
    assert row.status == "running"
    assert row.started_at == NOW + timedelta(minutes=1)


def test_freshness_keeps_cash_and_holdings_confirmation_times_separate(db_session):
    old = datetime(2026, 9, 22, 23, 0, tzinfo=UTC)
    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=old),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=old),
        ]
    )
    db_session.commit()
    check = NoNetworkDailyCheck(db_session, clock=lambda: NOW)

    freshness = check.validate_freshness(date(2026, 9, 23))

    assert freshness["cash_as_of"] == old
    assert freshness["holdings_as_of"] is None
    assert freshness["price_as_of"] is None
    snapshot = check.create_snapshot(date(2026, 9, 23), freshness)
    assert '"cash_confirmed_at": "2026-09-22T23:00:00+00:00"' in snapshot.details_json
    assert '"holdings_confirmed_at": null' in snapshot.details_json


def test_freshness_never_treats_intraday_estimate_as_official_nav(db_session):
    account = Account(name="cash", kind="cash")
    asset = Asset(code="000001", market="CN", name="Fund", asset_class="fund")
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add(
        Holding(account_id=account.id, asset_id=asset.id, quantity=Decimal(1))
    )
    db_session.add(
        PriceSnapshot(
            asset_id=asset.id,
            valuation_date=date(2026, 9, 23),
            price=Decimal("1.2"),
            source="efinance:estimate",
            quote_type="intraday_estimate",
            fetched_at=NOW,
        )
    )
    db_session.flush()

    freshness = DailyCheck(db_session, clock=lambda: NOW).validate_freshness(
        date(2026, 9, 23)
    )

    assert freshness["prices_fresh"] is False
    assert freshness["sources"] == []


def test_recorded_cash_and_official_nav_do_not_expire_by_age(db_session):
    old = NOW - timedelta(days=30)
    account = Account(name="cash", kind="cash")
    asset = Asset(code="000001", market="CN", name="Fund", asset_class="fund")
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add_all(
        [
            Holding(
                account_id=account.id,
                asset_id=asset.id,
                quantity=Decimal(1),
                updated_at=old,
            ),
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=old),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=old),
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=old.date(),
                price=Decimal("1.2"),
                source="manual:user-entry",
                quote_type="official_nav",
                fetched_at=old,
            ),
        ]
    )
    db_session.flush()

    freshness = DailyCheck(db_session, clock=lambda: NOW).validate_freshness(
        date(2026, 9, 23)
    )

    assert freshness["prices_fresh"] is True
    assert freshness["holdings_fresh"] is True
    assert freshness["cash_fresh"] is True


def test_freshness_prefers_manual_nav_over_newer_same_day_automatic_source(
    db_session,
):
    account = Account(name="cash", kind="cash")
    asset = Asset(code="000001", market="CN", name="Fund", asset_class="fund")
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add(Holding(account_id=account.id, asset_id=asset.id, quantity=Decimal(1)))
    db_session.add_all(
        [
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=date(2026, 9, 22),
                price=Decimal("1.1"),
                source="eastmoney",
                fetched_at=NOW - timedelta(minutes=1),
            ),
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=date(2026, 9, 22),
                price=Decimal("1.2"),
                source="manual:user-entry",
                fetched_at=NOW - timedelta(minutes=5),
            ),
        ]
    )
    db_session.flush()

    freshness = DailyCheck(db_session, clock=lambda: NOW).validate_freshness(
        date(2026, 9, 23)
    )

    assert freshness["sources"] == ["manual:user-entry"]
    assert freshness["price_as_of"] == NOW - timedelta(minutes=5)


def test_allocation_targets_trigger_satellite_reduction(db_session):
    db_session.add_all(
        [
            Asset(
                code="000001",
                market="CN",
                name="Satellite",
                portfolio_role=PortfolioRole.SATELLITE,
            ),
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW),
            AllocationTarget(name="core", target_bps=7000),
            AllocationTarget(name="satellite", target_bps=3000),
            AllocationTarget(name="satellite_each", target_bps=0, upper_bps=1000),
        ]
    )
    db_session.flush()
    asset = db_session.scalar(select(Asset))
    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=1_100_000,
        known_value_cents=1_100_000,
        invested_value_cents=100_000,
        cash_value_cents=1_000_000,
        data_complete=True,
        details_json=(
            f'{{"holdings":[{{"asset_id":{asset.id},"value_cents":6000}},'
            f'{{"asset_id":{asset.id},"value_cents":6000}}]}}'
        ),
    )
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    advice = DailyCheck(db_session, clock=lambda: NOW).evaluate_rules(
        snapshot, freshness
    )

    assert any(item.reason_code == "ABOVE_UPPER" for item in advice)


def test_two_satellite_reductions_keep_identity_and_warn_against_adding(db_session):
    assets = [
        Asset(
            code="SAT-A",
            market="CN",
            name="Satellite A",
            portfolio_role=PortfolioRole.SATELLITE,
        ),
        Asset(
            code="SAT-B",
            market="CN",
            name="Satellite B",
            portfolio_role=PortfolioRole.SATELLITE,
        ),
    ]
    db_session.add_all(
        assets
        + [
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW),
            AllocationTarget(name="satellite", target_bps=3000),
            AllocationTarget(name="satellite_each", target_bps=0, upper_bps=1000),
        ]
    )
    db_session.flush()
    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=120_000,
        known_value_cents=120_000,
        invested_value_cents=100_000,
        cash_value_cents=20_000,
        data_complete=True,
        details_json=json.dumps(
            {
                "holdings": [
                    {"asset_id": assets[0].id, "value_cents": 30_000},
                    {"asset_id": assets[1].id, "value_cents": 20_000},
                ]
            }
        ),
    )
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }
    check = DailyCheck(db_session, clock=lambda: NOW)

    advice = check.evaluate_rules(snapshot, freshness)
    reductions = [
        item for item in advice if item.action is AdviceAction.REDUCE_IN_BATCHES
    ]

    assert [
        (
            item.scope,
            item.code,
            item.name,
            item.current_bps,
            item.upper_bps,
        )
        for item in reductions
    ] == [
        ("portfolio_role", None, "satellite", 5000, 3000),
        ("asset", "SAT-A", "Satellite A", 3000, 1000),
        ("asset", "SAT-B", "Satellite B", 2000, 1000),
    ]
    body = check.build_digest(date(2026, 9, 23), snapshot, freshness, advice).body
    assert "对象：Satellite A（SAT-A）" in body
    assert "当前占比：30.00%；上限：10.00%" in body
    assert "整体与单基金减持金额不可累加" in body


def test_incomplete_snapshot_forces_wait_even_if_threshold_flags_are_true(db_session):
    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW),
        ]
    )
    db_session.flush()
    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=None,
        known_value_cents=120_000,
        data_complete=False,
    )
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    advice = DailyCheck(db_session, clock=lambda: NOW).evaluate_rules(
        snapshot, freshness
    )

    assert advice[0].action is AdviceAction.WAIT_FOR_DATA
    digest = DailyCheck(db_session, clock=lambda: NOW).build_digest(
        date(2026, 9, 23), snapshot, freshness, advice
    )
    assert "账本记录，待确认" not in digest.body
    assert "（待确认）" not in digest.body
    assert "（有效）" not in digest.body


def test_missing_monthly_budget_prohibits_buy_advice(db_session):
    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW),
        ]
    )
    db_session.flush()
    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=120_000,
        known_value_cents=120_000,
        invested_value_cents=1,
        cash_value_cents=119_999,
        data_complete=True,
        details_json='{"holdings":[]}',
    )
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    advice = DailyCheck(db_session, clock=lambda: NOW).evaluate_rules(
        snapshot, freshness
    )

    assert advice[0].action is AdviceAction.WAIT_FOR_DATA


def test_incomplete_monthly_budget_prohibits_buy_advice(db_session):
    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW),
            MonthlyBudget(
                month="2026-09", bucket_kind="investment", amount_cents=20_000
            ),
        ]
    )
    db_session.flush()
    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=120_000,
        known_value_cents=120_000,
        invested_value_cents=1,
        cash_value_cents=119_999,
        data_complete=True,
        details_json='{"holdings":[]}',
    )
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    advice = DailyCheck(db_session, clock=lambda: NOW).evaluate_rules(
        snapshot, freshness
    )

    assert advice[0].action is AdviceAction.WAIT_FOR_DATA


def test_monthly_budget_remaining_deducts_buy_amount_and_fee(db_session):
    account = Account(name="Broker", kind="investment")
    asset = Asset(code="BUDGET", market="CN", name="Budget test")
    db_session.add_all(
        [
            account,
            asset,
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(
                bucket_kind="investment", balance_cents=100_000, updated_at=NOW
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 20_000,
                "discretionary": 10_000,
            }.items()
        ]
        + [
            Transaction(
                source="test",
                external_id="september-buy",
                kind="BUY",
                account_id=account.id,
                asset_id=asset.id,
                amount_cents=6_000,
                quantity=Decimal(1),
                fee_cents=250,
                occurred_at=datetime(2026, 9, 20, tzinfo=UTC),
            )
        ]
    )
    db_session.flush()
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=200_000,
        known_value_cents=200_000,
        invested_value_cents=1,
        cash_value_cents=199_999,
        data_complete=True,
        details_json='{"holdings":[]}',
    )
    advice = DailyCheck(db_session, clock=lambda: NOW).evaluate_rules(
        snapshot, freshness
    )

    assert advice[0].action is AdviceAction.BUY
    assert advice[0].amount_cents == 13_750


def test_monthly_budget_remaining_stops_at_zero_when_exhausted(db_session):
    account = Account(name="Broker", kind="investment")
    asset = Asset(code="EXHAUSTED", market="CN", name="Exhausted budget")
    db_session.add_all(
        [
            account,
            asset,
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(
                bucket_kind="investment", balance_cents=100_000, updated_at=NOW
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 5_000,
                "discretionary": 10_000,
            }.items()
        ]
        + [
            Transaction(
                source="test",
                external_id="overspent-buy",
                kind="BUY",
                account_id=account.id,
                asset_id=asset.id,
                amount_cents=6_000,
                quantity=Decimal(1),
                fee_cents=250,
                occurred_at=datetime(2026, 9, 20, tzinfo=UTC),
            )
        ]
    )
    db_session.flush()
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=200_000,
        known_value_cents=200_000,
        invested_value_cents=1,
        cash_value_cents=199_999,
        data_complete=True,
        details_json='{"holdings":[]}',
    )
    advice = DailyCheck(db_session, clock=lambda: NOW).evaluate_rules(
        snapshot, freshness
    )

    assert advice[0].action is AdviceAction.WAIT
    assert advice[0].amount_cents == 0


def test_monthly_budget_usage_excludes_reversed_buys(db_session):
    account = Account(name="Broker", kind="investment")
    asset = Asset(code="REVERSED", market="CN", name="Reversed buy")
    db_session.add_all([account, asset])
    db_session.flush()
    original = Transaction(
        source="test",
        external_id="reversed-buy",
        kind="BUY",
        account_id=account.id,
        asset_id=asset.id,
        amount_cents=6_000,
        quantity=Decimal(1),
        fee_cents=250,
        occurred_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    db_session.add(original)
    db_session.flush()
    db_session.add_all(
        [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 20_000,
                "discretionary": 10_000,
            }.items()
        ]
        + [
            Transaction(
                source="test",
                external_id="reversal",
                kind="BUY",
                account_id=account.id,
                asset_id=asset.id,
                amount_cents=6_000,
                quantity=Decimal(1),
                fee_cents=250,
                occurred_at=datetime(2026, 9, 11, tzinfo=UTC),
                reverses_transaction_id=original.id,
            )
        ]
    )
    db_session.flush()
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    context = DailyCheck(db_session, clock=lambda: NOW)._rule_context(
        freshness, date(2026, 9, 23)
    )

    assert context.month_budget_remaining_cents == 20_000


def test_monthly_budget_uses_shanghai_month_boundaries(db_session):
    account = Account(name="Broker", kind="investment")
    asset = Asset(code="BOUNDARY", market="CN", name="Boundary buys")
    db_session.add_all([account, asset])
    db_session.flush()
    db_session.add_all(
        [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 20_000,
                "discretionary": 10_000,
            }.items()
        ]
        + [
            Transaction(
                source="test",
                external_id="before-cycle",
                kind="BUY",
                account_id=account.id,
                asset_id=asset.id,
                amount_cents=7_000,
                quantity=Decimal(1),
                fee_cents=0,
                occurred_at=datetime(2026, 9, 14, 15, 30, tzinfo=UTC),
            ),
            Transaction(
                source="test",
                external_id="cycle-start",
                kind="BUY",
                account_id=account.id,
                asset_id=asset.id,
                amount_cents=3_000,
                quantity=Decimal(1),
                fee_cents=0,
                occurred_at=datetime(2026, 9, 14, 16, 30, tzinfo=UTC),
            ),
        ]
    )
    db_session.flush()
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:statement"],
    }

    context = DailyCheck(db_session, clock=lambda: NOW)._rule_context(
        freshness, date(2026, 9, 23)
    )

    assert context.month_budget_remaining_cents == 17_000


def test_fresh_reserve_shortfall_survives_missing_holdings_and_alerts(db_session):
    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=50_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW),
        ]
        + [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 20_000,
                "discretionary": 10_000,
            }.items()
        ]
    )
    db_session.commit()
    check = RealRuleRecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS

    assert [item.reason_code for item in check.advice] == [
        "DATA_UNCONFIRMED",
        "RESERVE_SHORTFALL",
    ]
    assert [item.action for item in check.advice] == [
        AdviceAction.WAIT_FOR_DATA,
        AdviceAction.WAIT,
    ]
    assert [critical for _title, critical in check.notifications] == [False, True]


def test_recorded_cash_age_does_not_hide_reserve_shortfall(db_session):
    stale = NOW - timedelta(days=2)
    db_session.add_all(
        [
            CashBucket(bucket_kind="reserve", balance_cents=50_000, updated_at=stale),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=stale),
        ]
        + [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 20_000,
                "discretionary": 10_000,
            }.items()
        ]
    )
    db_session.commit()
    check = RealRuleRecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS

    assert [item.reason_code for item in check.advice] == [
        "DATA_UNCONFIRMED",
        "RESERVE_SHORTFALL",
    ]
    assert [critical for _title, critical in check.notifications] == [False, True]


def test_unknown_reserve_does_not_fabricate_shortfall(db_session):
    db_session.add_all(
        [CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW)]
        + [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 20_000,
                "discretionary": 10_000,
            }.items()
        ]
    )
    db_session.commit()
    check = RealRuleRecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.SUCCESS

    assert [item.reason_code for item in check.advice] == ["DATA_UNCONFIRMED"]
    assert [critical for _title, critical in check.notifications] == [False]


def test_scheduled_outside_five_minute_window_changes_nothing(db_session):
    db_session.add(AppSetting(key="daily_schedule", value="09:10"))
    db_session.commit()
    check = RecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run_scheduled() is DailyCheckResult.NOT_DUE

    assert check.events == []
    assert db_session.scalar(select(JobRun)) is None


@pytest.mark.parametrize("day", [date(2026, 9, 26), date(2026, 9, 27)])
def test_scheduled_check_skips_weekends(db_session, day):
    at_time = datetime(day.year, day.month, day.day, 6, 0, tzinfo=UTC)
    check = RecordingDailyCheck(db_session, clock=lambda: at_time)
    assert check.run_scheduled() is DailyCheckResult.NOT_DUE
    assert check.events == []


def test_scheduled_check_skips_configured_chinese_holiday(db_session):
    db_session.add(AppSetting(key="cn_holidays", value='["2026-10-01"]'))
    db_session.commit()
    at_time = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
    check = RecordingDailyCheck(db_session, clock=lambda: at_time)
    assert check.run_scheduled() is DailyCheckResult.NOT_DUE
    assert check.events == []


def test_opportunity_scan_uses_special_budget_once_for_tracked_unheld_fund(db_session):
    asset = Asset(
        code="000001",
        market="CN",
        name="Candidate",
        asset_class="fund",
        portfolio_role=PortfolioRole.CORE,
    )
    db_session.add_all(
        [
            asset,
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=0, updated_at=NOW),
            AllocationTarget(name="core", target_bps=7000),
        ]
        + [
            MonthlyBudget(month="2026-09", bucket_kind=kind, amount_cents=amount)
            for kind, amount in {
                "fixed_expense": 270_000,
                "reserve": 100_000,
                "investment": 20_000,
                "discretionary": 10_000,
            }.items()
        ]
    )
    db_session.flush()
    start = date(2026, 1, 16)
    for index in range(250):
        value = "1.50" if index < 100 else "1.70" if index < 200 else "1.65"
        db_session.add(
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=start + timedelta(days=index),
                source="manual:history",
                price=Decimal(value),
                quote_type="official_nav",
                fetched_at=NOW,
            )
        )
    db_session.flush()
    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=100_000,
        known_value_cents=100_000,
        invested_value_cents=100_000,
        cash_value_cents=0,
        data_complete=True,
        details_json='{"holdings":[]}',
    )
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:history"],
    }
    check = DailyCheck(db_session, clock=lambda: NOW)

    advice = check.scan_opportunities(snapshot, freshness, date(2026, 9, 23))
    db_session.flush()

    assert advice[0].action is AdviceAction.BUY_IN_BATCHES
    assert advice[0].amount_cents == 10_000
    record = db_session.scalar(select(OpportunityAlert))
    assert record is not None and record.used_special_budget is True
    assert check.scan_opportunities(snapshot, freshness, date(2026, 9, 23)) == []


def test_opportunity_history_uses_one_priority_sample_per_valuation_date(
    db_session, monkeypatch
):
    asset = Asset(
        code="000002",
        market="CN",
        name="Corrected Candidate",
        asset_class="fund",
        portfolio_role=PortfolioRole.CORE,
    )
    db_session.add_all(
        [
            asset,
            CashBucket(bucket_kind="reserve", balance_cents=100_000, updated_at=NOW),
            CashBucket(bucket_kind="investment", balance_cents=20_000, updated_at=NOW),
            AllocationTarget(name="core", target_bps=7000),
            MonthlyBudget(month="2026-09", bucket_kind="investment", amount_cents=20_000),
        ]
    )
    db_session.flush()
    start = date(2026, 1, 16)
    for index in range(249):
        value = "1.50" if index < 100 else "1.70"
        db_session.add(
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=start + timedelta(days=index),
                source="efinance",
                price=Decimal(value),
                fetched_at=NOW - timedelta(days=1),
            )
        )
    latest_date = start + timedelta(days=249)
    db_session.add_all(
        [
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=latest_date,
                source="manual:user-entry",
                price=Decimal("1.50"),
                fetched_at=NOW - timedelta(minutes=3),
            ),
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=latest_date,
                source="eastmoney",
                price=Decimal("9.00"),
                fetched_at=NOW - timedelta(minutes=1),
            ),
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=latest_date,
                source="efinance",
                price=Decimal("8.00"),
                fetched_at=NOW - timedelta(minutes=2),
            ),
        ]
    )
    db_session.flush()
    snapshot = PortfolioSnapshot(
        snapshot_date=date(2026, 9, 23),
        total_value_cents=100_000,
        known_value_cents=100_000,
        invested_value_cents=100_000,
        cash_value_cents=0,
        data_complete=True,
        details_json='{"holdings":[]}',
    )
    freshness = {
        "prices_fresh": True,
        "holdings_fresh": True,
        "cash_fresh": True,
        "as_of": NOW,
        "sources": ["manual:user-entry"],
    }
    candidates = []

    def record_candidate(candidate):
        candidates.append(candidate)
        return evaluate_opportunity(candidate)

    monkeypatch.setattr("finance_app.cli.evaluate_opportunity", record_candidate)

    advice = DailyCheck(db_session, clock=lambda: NOW).scan_opportunities(
        snapshot, freshness, date(2026, 9, 23)
    )

    assert candidates[0].history_days == 250
    assert advice[0].source == "manual:user-entry"


def test_scheduled_runs_inside_beijing_five_minute_window(db_session):
    db_session.add(AppSetting(key="daily_schedule", value="09:00"))
    db_session.commit()
    check = RecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run_scheduled() is DailyCheckResult.SUCCESS

    assert db_session.scalar(select(JobRun)).business_date == date(2026, 9, 23)


def test_scheduled_window_crossing_midnight_uses_scheduled_business_date(db_session):
    after_midnight = datetime(2026, 9, 23, 16, 1, tzinfo=UTC)  # Beijing 00:01 Sep 24
    db_session.add(AppSetting(key="daily_schedule", value="23:58"))
    db_session.commit()
    check = RecordingDailyCheck(db_session, clock=lambda: after_midnight)

    assert check.run_scheduled() is DailyCheckResult.SUCCESS

    assert db_session.scalar(select(JobRun)).business_date == date(2026, 9, 23)


def test_dry_run_never_persists_or_sends(db_session):
    check = RecordingDailyCheck(db_session, clock=lambda: NOW, dry_run=True)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.DRY_RUN

    assert check.events == [
        "validate_freshness",
        "snapshot",
        "evaluate_rules",
    ]
    assert check.notifications == []
    assert db_session.scalar(select(JobRun)) is None
    assert db_session.scalar(select(NotificationDelivery)) is None


def test_dry_run_rolls_back_generated_snapshot(db_session):
    check = NoNetworkDailyCheck(db_session, clock=lambda: NOW, dry_run=True)

    assert check.run(date(2026, 9, 23)) is DailyCheckResult.DRY_RUN

    assert db_session.scalar(select(PortfolioSnapshot)) is None


def test_partial_notification_failure_is_recorded_per_channel(db_session, monkeypatch):
    email_channel = NotificationChannel(
        name="Primary email", channel_type="email", enabled=True
    )
    wechat_channel = NotificationChannel(
        name="WeChat backup", channel_type="pushplus", enabled=True
    )
    db_session.add_all([email_channel, wechat_channel])
    db_session.commit()
    email = StubNotifier("email", DeliveryResult.success(provider="email"))
    wechat = StubNotifier(
        "pushplus",
        DeliveryResult.failed(
            provider="pushplus",
            error_code="provider_rejected",
            error_summary="provider_rejected: rejected delivery",
            retryable=False,
        ),
    )
    monkeypatch.setattr("finance_app.cli.EmailNotifier", lambda _settings: email)
    monkeypatch.setattr(
        "finance_app.cli.build_wechat_notifier",
        lambda _channel, _settings: wechat,
    )

    status = DailyCheck(db_session, clock=lambda: NOW).notify(
        Notification("Digest", "Body")
    )

    assert status is DeliveryStatus.FAILED
    assert email.calls == 1 and wechat.calls == 1
    assert [
        (row.channel_id, row.status)
        for row in db_session.scalars(
            select(NotificationDelivery).order_by(NotificationDelivery.channel_id)
        )
    ] == [(email_channel.id, "success"), (wechat_channel.id, "failed")]

    wechat.result = DeliveryResult.success(provider="pushplus")
    assert DailyCheck(db_session, clock=lambda: NOW).notify(
        Notification("Digest", "Body")
    ) is DeliveryStatus.SUCCESS
    assert email.calls == 1
    assert wechat.calls == 2
    assert [
        row.status
        for row in db_session.scalars(
            select(NotificationDelivery).order_by(NotificationDelivery.id)
        )
    ] == ["success", "failed", "success"]


def test_cli_rejects_invalid_date_without_opening_database(monkeypatch, capsys):
    monkeypatch.setattr(
        "finance_app.cli.get_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    assert main(["daily-check", "--date", "2026-02-30"]) == 2
    assert "valid ISO date" in capsys.readouterr().err
