from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from finance_app.cli import main
from finance_app.ledger.models import Account, Asset, Holding
from finance_app.market.base import FundNavQuote, QuoteType
from finance_app.market.nav_job import NavSyncResult, OfficialNavSyncJob
from finance_app.notifications.models import AppSetting
from finance_app.portfolio.models import PriceSnapshot
from tests.test_database import db_session as database_session_fixture

db_session = database_session_fixture

EVENING_NOW = datetime.fromisoformat("2026-09-29T20:00:00+08:00")


class PerCodeProvider:
    source = "test:official-nav"
    source_url = "https://example.test/nav"

    def __init__(self, *, failing_codes: set[str] | None = None) -> None:
        self.failing_codes = failing_codes or set()
        self.calls: list[str] = []

    def fetch(self, fund_code: str) -> FundNavQuote:
        self.calls.append(fund_code)
        if fund_code in self.failing_codes:
            raise RuntimeError(f"provider failed for {fund_code}")
        return FundNavQuote(
            value=Decimal("1.2345"),
            valuation_date=date(2026, 9, 29),
            source=self.source,
            source_url=self.source_url,
            fetched_at=EVENING_NOW,
            quote_type=QuoteType.OFFICIAL_NAV,
        )


class PerCodeHistoryProvider:
    source = "test:history"
    source_url = "https://example.test/history"

    def __init__(self, *, failing_codes: set[str] | None = None) -> None:
        self.failing_codes = failing_codes or set()
        self.calls: list[str] = []

    def fetch_history(
        self, fund_code: str, *, limit: int = 500
    ) -> list[FundNavQuote]:
        self.calls.append(fund_code)
        if fund_code in self.failing_codes:
            raise RuntimeError(f"history failed for {fund_code}")
        return [
            FundNavQuote(
                value=Decimal("1.1111"),
                valuation_date=date(2026, 9, 28),
                source=self.source,
                source_url=self.source_url,
                fetched_at=EVENING_NOW,
                quote_type=QuoteType.OFFICIAL_NAV,
            )
        ]


def seed_refresh_scope(db_session, *, include_second: bool = True):
    first_account = Account(name="Primary", kind="investment")
    second_account = Account(name="Secondary", kind="investment")
    first = Asset(code="000001", market="CN", name="First", asset_class="fund")
    second = Asset(code="000002", market="CN", name="Second", asset_class="fund")
    cleared = Asset(code="000003", market="CN", name="Cleared", asset_class="fund")
    stock = Asset(code="600000", market="CN", name="Stock", asset_class="stock")
    overseas = Asset(code="000004", market="HK", name="HK Fund", asset_class="fund")
    db_session.add_all(
        [first_account, second_account, first, second, cleared, stock, overseas]
    )
    db_session.flush()
    holdings = [
        Holding(account_id=first_account.id, asset_id=first.id, quantity=Decimal(10)),
        Holding(account_id=second_account.id, asset_id=first.id, quantity=Decimal(5)),
        Holding(account_id=first_account.id, asset_id=cleared.id, quantity=Decimal(0)),
        Holding(account_id=first_account.id, asset_id=stock.id, quantity=Decimal(2)),
        Holding(account_id=first_account.id, asset_id=overseas.id, quantity=Decimal(3)),
    ]
    if include_second:
        holdings.append(
            Holding(
                account_id=first_account.id,
                asset_id=second.id,
                quantity=Decimal(8),
            )
        )
    db_session.add_all(holdings)
    db_session.commit()
    return first, second


def test_job_requires_an_official_nav_provider(db_session):
    with pytest.raises(ValueError, match="official NAV provider"):
        OfficialNavSyncJob(db_session, providers=[])


def test_job_refreshes_distinct_positive_held_cn_funds(db_session):
    first, second = seed_refresh_scope(db_session)
    primary = PerCodeProvider()

    result = OfficialNavSyncJob(
        db_session,
        providers=[primary],
        clock=lambda: EVENING_NOW,
    ).run_scheduled()

    assert primary.calls == [first.code, second.code]
    assert result == NavSyncResult(
        status="success", attempted=2, succeeded=2, unchanged=0, failed=0
    )
    assert {
        row.quote_type for row in db_session.scalars(select(PriceSnapshot))
    } == {QuoteType.OFFICIAL_NAV.value}


def test_job_uses_fallback_after_primary_failure(db_session):
    first, _ = seed_refresh_scope(db_session, include_second=False)
    primary = PerCodeProvider(failing_codes={first.code})
    fallback = PerCodeProvider()
    fallback.source = "test:fallback"

    result = OfficialNavSyncJob(
        db_session, providers=[primary, fallback], clock=lambda: EVENING_NOW
    ).refresh_held_funds()

    assert result == NavSyncResult("success", 1, 1, 0, 0)
    assert primary.calls == [first.code]
    assert fallback.calls == [first.code]
    assert db_session.scalar(select(PriceSnapshot.source)) == "test:fallback"


def test_job_counts_repeat_observation_as_unchanged(db_session):
    seed_refresh_scope(db_session, include_second=False)
    provider = PerCodeProvider()
    job = OfficialNavSyncJob(
        db_session, providers=[provider], clock=lambda: EVENING_NOW
    )

    assert job.refresh_held_funds().succeeded == 1
    second = job.refresh_held_funds()

    assert second == NavSyncResult("success", 1, 0, 1, 0)
    assert db_session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1


def test_one_fund_failure_does_not_stop_later_funds(db_session):
    first, second = seed_refresh_scope(db_session)
    provider = PerCodeProvider(failing_codes={first.code})

    result = OfficialNavSyncJob(
        db_session, providers=[provider], clock=lambda: EVENING_NOW
    ).refresh_held_funds()

    assert provider.calls == [first.code, second.code]
    assert result == NavSyncResult("success", 2, 1, 0, 1)
    assert db_session.scalar(select(PriceSnapshot.asset_id)) == second.id


def test_job_reports_failure_only_when_every_attempt_fails(db_session):
    first, second = seed_refresh_scope(db_session)
    provider = PerCodeProvider(failing_codes={first.code, second.code})

    result = OfficialNavSyncJob(
        db_session, providers=[provider], clock=lambda: EVENING_NOW
    ).refresh_held_funds()

    assert result == NavSyncResult("failed", 2, 0, 0, 2)


def test_job_with_no_holdings_succeeds_with_zero_counts(db_session):
    result = OfficialNavSyncJob(
        db_session, providers=[PerCodeProvider()], clock=lambda: EVENING_NOW
    ).refresh_held_funds()

    assert result == NavSyncResult("success", 0, 0, 0, 0)


def test_job_backfills_history_for_unheld_tracked_fund(db_session):
    candidate = Asset(
        code="000099", market="CN", name="Candidate", asset_class="fund"
    )
    db_session.add(candidate)
    db_session.commit()
    history = PerCodeHistoryProvider()

    result = OfficialNavSyncJob(
        db_session,
        providers=[PerCodeProvider()],
        history_provider=history,
        clock=lambda: EVENING_NOW,
    ).run_scheduled()

    assert result == NavSyncResult("success", 0, 0, 0, 0)
    assert history.calls == [candidate.code]
    row = db_session.scalar(select(PriceSnapshot))
    assert row is not None
    assert row.asset_id == candidate.id
    assert row.quote_type == QuoteType.OFFICIAL_NAV.value


def test_history_failure_for_one_tracked_fund_does_not_block_another(db_session):
    failed = Asset(code="000098", market="CN", name="Failed", asset_class="fund")
    succeeded = Asset(
        code="000099", market="CN", name="Succeeded", asset_class="fund"
    )
    db_session.add_all([failed, succeeded])
    db_session.commit()
    history = PerCodeHistoryProvider(failing_codes={failed.code})

    OfficialNavSyncJob(
        db_session,
        providers=[PerCodeProvider()],
        history_provider=history,
        clock=lambda: EVENING_NOW,
    ).run_scheduled()

    assert history.calls == [failed.code, succeeded.code]
    rows = list(db_session.scalars(select(PriceSnapshot)))
    assert [row.asset_id for row in rows] == [succeeded.id]


def test_evening_holiday_is_skipped_but_morning_backfill_runs(db_session):
    seed_refresh_scope(db_session)
    db_session.add(AppSetting(key="cn_holidays", value='["2026-10-01"]'))
    db_session.commit()

    evening_provider = PerCodeProvider()
    morning_provider = PerCodeProvider()
    evening = OfficialNavSyncJob(
        db_session,
        providers=[evening_provider],
        clock=lambda: datetime.fromisoformat("2026-10-01T20:00:00+08:00"),
    ).run_scheduled()
    morning = OfficialNavSyncJob(
        db_session,
        providers=[morning_provider],
        clock=lambda: datetime.fromisoformat("2026-10-02T08:00:00+08:00"),
    ).run_scheduled()

    assert evening == NavSyncResult("skipped", 0, 0, 0, 0)
    assert evening_provider.calls == []
    assert morning.status == "success"
    assert morning_provider.calls == ["000001", "000002"]


@pytest.mark.parametrize(
    "local_time",
    ["2026-10-03T20:00:00+08:00", "2026-10-04T20:00:00+08:00"],
)
def test_weekend_evening_is_skipped(db_session, local_time):
    seed_refresh_scope(db_session)
    provider = PerCodeProvider()

    result = OfficialNavSyncJob(
        db_session,
        providers=[provider],
        clock=lambda: datetime.fromisoformat(local_time),
    ).run_scheduled()

    assert result == NavSyncResult("skipped", 0, 0, 0, 0)
    assert provider.calls == []


@pytest.mark.parametrize(
    ("job_result", "expected_exit"),
    [
        (NavSyncResult("success", 2, 1, 1, 0), 0),
        (NavSyncResult("success", 0, 0, 0, 0), 0),
        (NavSyncResult("skipped", 0, 0, 0, 0), 0),
        (NavSyncResult("failed", 2, 0, 0, 2), 1),
    ],
)
def test_nav_refresh_cli_reports_counts_and_closes_primary(
    monkeypatch, capsys, job_result, expected_exit
):
    class Primary:
        closed = False

        def close(self):
            self.closed = True

    primary = Primary()
    fallback = object()

    class Job:
        def __init__(self, session, *, providers):
            assert session == "session"
            assert providers == [primary, fallback]

        def run_scheduled(self):
            return job_result

    monkeypatch.setattr(
        "finance_app.cli.get_session_factory",
        lambda: lambda: nullcontext("session"),
    )
    monkeypatch.setattr("finance_app.cli.EastMoneyFundNavProvider", lambda: primary)
    monkeypatch.setattr("finance_app.cli.EfinanceAdapter", lambda: fallback)
    monkeypatch.setattr("finance_app.cli.OfficialNavSyncJob", Job)

    assert main(["nav-refresh", "--scheduled"]) == expected_exit

    assert primary.closed is True
    assert capsys.readouterr().out.strip() == (
        f"nav-refresh status={job_result.status} attempted={job_result.attempted} "
        f"succeeded={job_result.succeeded} unchanged={job_result.unchanged} "
        f"failed={job_result.failed}"
    )


def test_nav_refresh_cli_closes_primary_and_sanitizes_internal_error(
    monkeypatch, capsys
):
    class Primary:
        closed = False

        def close(self):
            self.closed = True

    primary = Primary()

    class Job:
        def __init__(self, session, *, providers):
            pass

        def run_scheduled(self):
            raise RuntimeError("sensitive upstream response body")

    monkeypatch.setattr(
        "finance_app.cli.get_session_factory",
        lambda: lambda: nullcontext("session"),
    )
    monkeypatch.setattr("finance_app.cli.EastMoneyFundNavProvider", lambda: primary)
    monkeypatch.setattr("finance_app.cli.EfinanceAdapter", lambda: object())
    monkeypatch.setattr("finance_app.cli.OfficialNavSyncJob", Job)

    assert main(["nav-refresh", "--scheduled"]) == 1

    captured = capsys.readouterr()
    assert primary.closed is True
    assert captured.out == ""
    assert captured.err.strip() == "nav-refresh status=failed error=internal_error"
    assert "sensitive" not in captured.err


def test_nav_refresh_cli_sanitizes_primary_close_error(monkeypatch, capsys):
    class Primary:
        def close(self):
            raise RuntimeError("sensitive close failure")

    class Job:
        def __init__(self, session, *, providers):
            pass

        def run_scheduled(self):
            return NavSyncResult("success", 1, 1, 0, 0)

    monkeypatch.setattr(
        "finance_app.cli.get_session_factory",
        lambda: lambda: nullcontext("session"),
    )
    monkeypatch.setattr("finance_app.cli.EastMoneyFundNavProvider", Primary)
    monkeypatch.setattr("finance_app.cli.EfinanceAdapter", lambda: object())
    monkeypatch.setattr("finance_app.cli.OfficialNavSyncJob", Job)

    assert main(["nav-refresh", "--scheduled"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "nav-refresh status=failed error=internal_error"
    assert "sensitive" not in captured.err
