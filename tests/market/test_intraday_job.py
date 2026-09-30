from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from finance_app.cli import main
from finance_app.ledger.models import Account, Asset, Holding
from finance_app.market.base import FundNavQuote, QuoteType
from finance_app.market.intraday_job import (
    IntradayJobResult,
    IntradayRefreshJob,
    is_intraday_refresh_time,
)
from finance_app.notifications.models import AppSetting
from finance_app.portfolio.models import PriceSnapshot
from tests.test_database import db_session as database_session_fixture

db_session = database_session_fixture

TRADING_NOW = datetime.fromisoformat("2026-09-29T14:00:00+08:00")


class PerCodeProvider:
    source = "test:estimate"
    source_url = "https://example.test/estimate"

    def __init__(self, *, failing_code: str | None = None) -> None:
        self.failing_code = failing_code
        self.calls: list[str] = []

    def fetch(self, fund_code: str) -> FundNavQuote:
        self.calls.append(fund_code)
        if fund_code == self.failing_code:
            raise RuntimeError("provider failed")
        return FundNavQuote(
            value=Decimal("1.2345"),
            valuation_date=TRADING_NOW.date(),
            source=self.source,
            source_url=self.source_url,
            fetched_at=TRADING_NOW,
            quote_type=QuoteType.INTRADAY_ESTIMATE,
        )


@pytest.mark.parametrize(
    "local_time",
    [
        "2026-09-29T09:30:00+08:00",
        "2026-09-29T11:30:00+08:00",
        "2026-09-29T11:30:59+08:00",
        "2026-09-29T13:00:00+08:00",
        "2026-09-29T15:00:00+08:00",
        "2026-09-29T15:00:59+08:00",
    ],
)
def test_trading_window_includes_session_boundaries(local_time: str):
    assert is_intraday_refresh_time(datetime.fromisoformat(local_time), frozenset())


@pytest.mark.parametrize(
    "local_time",
    [
        "2026-09-29T09:29:00+08:00",
        "2026-09-29T11:31:00+08:00",
        "2026-09-29T12:30:00+08:00",
        "2026-09-29T15:01:00+08:00",
        "2026-10-03T10:00:00+08:00",
    ],
)
def test_trading_window_excludes_closed_times(local_time: str):
    assert not is_intraday_refresh_time(datetime.fromisoformat(local_time), frozenset())


def test_trading_window_converts_aware_timestamp_to_shanghai():
    utc_time = datetime.fromisoformat("2026-09-29T01:30:00+00:00")

    assert is_intraday_refresh_time(utc_time, frozenset())


def test_configured_holiday_is_skipped():
    now = datetime.fromisoformat("2026-10-01T14:00:00+08:00")

    assert not is_intraday_refresh_time(now, frozenset({date(2026, 10, 1)}))


def _seed_refresh_scope(db_session):
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
    db_session.add_all(
        [
            Holding(
                account_id=first_account.id,
                asset_id=first.id,
                quantity=Decimal(10),
            ),
            Holding(
                account_id=second_account.id,
                asset_id=first.id,
                quantity=Decimal(5),
            ),
            Holding(
                account_id=first_account.id,
                asset_id=second.id,
                quantity=Decimal(8),
            ),
            Holding(
                account_id=first_account.id,
                asset_id=cleared.id,
                quantity=Decimal(0),
            ),
            Holding(
                account_id=first_account.id,
                asset_id=stock.id,
                quantity=Decimal(2),
            ),
            Holding(
                account_id=first_account.id,
                asset_id=overseas.id,
                quantity=Decimal(3),
            ),
        ]
    )
    db_session.commit()
    return first, second


def test_job_refreshes_distinct_held_cn_funds_and_isolates_failures(db_session):
    first, second = _seed_refresh_scope(db_session)
    provider = PerCodeProvider(failing_code=second.code)

    result = IntradayRefreshJob(
        db_session, providers=[provider], clock=lambda: TRADING_NOW
    ).run_scheduled()

    assert result.status == "success"
    assert result.attempted == 2
    assert result.succeeded == 1
    assert result.failed == 1
    assert provider.calls == [first.code, second.code]
    snapshots = db_session.scalars(select(PriceSnapshot)).all()
    assert len(snapshots) == 1
    assert snapshots[0].asset_id == first.id
    assert snapshots[0].quote_type == QuoteType.INTRADAY_ESTIMATE


def test_job_skips_without_calling_provider_outside_trading_window(db_session):
    _seed_refresh_scope(db_session)
    provider = PerCodeProvider()
    closed = datetime.fromisoformat("2026-09-29T12:00:00+08:00")

    result = IntradayRefreshJob(
        db_session, providers=[provider], clock=lambda: closed
    ).run_scheduled()

    assert result.status == "skipped"
    assert (result.attempted, result.succeeded, result.failed) == (0, 0, 0)
    assert provider.calls == []


def test_job_reads_configured_holidays_before_refreshing(db_session):
    _seed_refresh_scope(db_session)
    db_session.add(AppSetting(key="cn_holidays", value='["2026-09-29"]'))
    db_session.commit()
    provider = PerCodeProvider()

    result = IntradayRefreshJob(
        db_session, providers=[provider], clock=lambda: TRADING_NOW
    ).run_scheduled()

    assert result.status == "skipped"
    assert provider.calls == []


def test_job_reports_failure_when_all_held_funds_fail(db_session):
    _first, second = _seed_refresh_scope(db_session)

    class AlwaysFailProvider(PerCodeProvider):
        def fetch(self, fund_code: str) -> FundNavQuote:
            self.calls.append(fund_code)
            raise RuntimeError(f"failed {fund_code}")

    provider = AlwaysFailProvider()
    result = IntradayRefreshJob(
        db_session, providers=[provider], clock=lambda: TRADING_NOW
    ).run_scheduled()

    assert second.code in provider.calls
    assert result.status == "failed"
    assert (result.attempted, result.succeeded, result.failed) == (2, 0, 2)


@pytest.mark.parametrize(
    ("job_result", "expected_exit"),
    [
        (IntradayJobResult("success", 2, 2, 0), 0),
        (IntradayJobResult("success", 0, 0, 0), 0),
        (IntradayJobResult("skipped", 0, 0, 0), 0),
        (IntradayJobResult("failed", 2, 0, 2), 1),
    ],
)
def test_market_refresh_cli_reports_compact_counts_and_closes_primary(
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
    monkeypatch.setattr("finance_app.cli.TiantianEstimateProvider", lambda: primary)
    monkeypatch.setattr("finance_app.cli.EfinanceEstimateAdapter", lambda: fallback)
    monkeypatch.setattr("finance_app.cli.IntradayRefreshJob", Job)

    assert main(["market-refresh", "--scheduled"]) == expected_exit

    assert primary.closed is True
    assert capsys.readouterr().out.strip() == (
        f"market-refresh status={job_result.status} "
        f"attempted={job_result.attempted} succeeded={job_result.succeeded} "
        f"failed={job_result.failed}"
    )


def test_market_refresh_cli_closes_primary_and_sanitizes_internal_error(
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
    monkeypatch.setattr("finance_app.cli.TiantianEstimateProvider", lambda: primary)
    monkeypatch.setattr("finance_app.cli.EfinanceEstimateAdapter", lambda: object())
    monkeypatch.setattr("finance_app.cli.IntradayRefreshJob", Job)

    assert main(["market-refresh", "--scheduled"]) == 1

    captured = capsys.readouterr()
    assert primary.closed is True
    assert captured.out == ""
    assert captured.err.strip() == "market-refresh status=failed error=internal_error"
    assert "sensitive" not in captured.err
