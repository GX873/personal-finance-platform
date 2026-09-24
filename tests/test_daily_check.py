from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update

from finance_app.cli import DailyCheck, DailyCheckResult, main
from finance_app.ledger.models import Asset, CashBucket, PortfolioRole
from finance_app.notifications.base import DeliveryResult, DeliveryStatus, Notification
from finance_app.notifications.models import (
    AppSetting,
    JobRun,
    NotificationChannel,
    NotificationDelivery,
)
from finance_app.portfolio.models import AllocationTarget, PortfolioSnapshot
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
    assert "账本记录，待确认" in digest.body
    assert "（有效）" not in digest.body


def test_scheduled_outside_five_minute_window_changes_nothing(db_session):
    db_session.add(AppSetting(key="daily_schedule", value="09:10"))
    db_session.commit()
    check = RecordingDailyCheck(db_session, clock=lambda: NOW)

    assert check.run_scheduled() is DailyCheckResult.NOT_DUE

    assert check.events == []
    assert db_session.scalar(select(JobRun)) is None


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
