from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_app.db import Base, create_db_engine
from finance_app.ledger.models import Account, Asset, AuditEvent, Holding
from finance_app.market.base import FundNavQuote, MarketDataError, QuoteType
from finance_app.market.eastmoney import EastMoneyFundNavProvider
from finance_app.market.service import FundPriceService, RefreshStatus
from finance_app.portfolio.models import PortfolioSnapshot, PriceSnapshot
from finance_app.portfolio.service import create_daily_snapshot

NOW = datetime(2026, 9, 23, 0, tzinfo=UTC)  # 08:00 in Asia/Shanghai
SOURCE_URL = (
    "https://api.fund.eastmoney.com/f10/lsjz"
    "?fundCode=000001&pageIndex=1&pageSize=1"
)


class StubProvider:
    source = "eastmoney"
    source_url = "https://api.fund.eastmoney.com/f10/lsjz"

    def __init__(self, quote: FundNavQuote | None = None, error: Exception | None = None):
        self.quote = quote
        self.error = error
        self.calls: list[str] = []

    def fetch(self, fund_code: str) -> FundNavQuote:
        self.calls.append(fund_code)
        if self.error is not None:
            raise self.error
        assert self.quote is not None
        return self.quote


class EfinanceStubProvider(StubProvider):
    source = "efinance"
    source_url = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHisNetList"


def quote(
    valuation_date: date = date(2026, 9, 22), *, attempts: int = 1
) -> FundNavQuote:
    return FundNavQuote(
        value=Decimal("1.2345"),
        valuation_date=valuation_date,
        source="eastmoney",
        source_url=SOURCE_URL,
        fetched_at=NOW,
        attempts=attempts,
    )


def setup_session() -> tuple[Session, Asset]:
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    asset = Asset(
        code="000001", market="CN", name="Test Fund", asset_class="fund"
    )
    session.add(asset)
    session.flush()
    return session, asset


def test_refresh_persists_provenance_and_reports_shanghai_freshness():
    session, asset = setup_session()
    try:
        result = FundPriceService(
            session, StubProvider(quote(attempts=3)), clock=lambda: NOW
        ).refresh("000001")

        row = session.scalar(select(PriceSnapshot))
        assert result.status is RefreshStatus.SUCCESS
        assert result.is_fresh is True
        assert result.snapshot is row
        assert result.error_summary is None
        assert result.attempts == 3
        assert row is not None
        assert row.asset_id == asset.id
        assert row.price == Decimal("1.23450000")
        assert row.valuation_date == date(2026, 9, 22)
        assert row.source == "eastmoney"
        assert row.source_url == SOURCE_URL
        assert row.fetched_at == NOW
        assert row.error_text is None
    finally:
        session.close()


def test_stale_daily_nav_is_persisted_but_never_presented_as_realtime():
    session, _ = setup_session()
    try:
        evening = datetime(2026, 9, 23, 14, tzinfo=UTC)  # 22:00 Shanghai
        stale_quote = FundNavQuote(
            value=Decimal("1.2345"),
            valuation_date=date(2026, 9, 22),
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=evening,
        )
        result = FundPriceService(
            session, StubProvider(stale_quote), clock=lambda: evening
        ).refresh("000001")

        assert result.status is RefreshStatus.STALE
        assert result.is_fresh is False
        assert result.snapshot is not None
        assert result.snapshot.valuation_date == date(2026, 9, 22)
    finally:
        session.close()


def test_freshness_uses_time_after_the_fetch_completes():
    session, _ = setup_session()
    try:
        timeline = {"now": NOW}
        fetched_at = datetime(2026, 9, 23, 0, 0, 1, tzinfo=UTC)

        class AdvancingProvider(StubProvider):
            def fetch(self, fund_code: str) -> FundNavQuote:
                timeline["now"] = fetched_at
                return super().fetch(fund_code)

        completed_quote = FundNavQuote(
            value=Decimal("1.2345"),
            valuation_date=date(2026, 9, 22),
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=fetched_at,
        )
        result = FundPriceService(
            session,
            AdvancingProvider(completed_quote),
            clock=lambda: timeline["now"],
        ).refresh("000001")

        assert result.status is RefreshStatus.SUCCESS
        assert result.is_fresh is True
    finally:
        session.close()


def test_provider_failure_does_not_overwrite_or_delete_last_good_price():
    session, asset = setup_session()
    try:
        original = PriceSnapshot(
            asset_id=asset.id,
            valuation_date=date(2026, 9, 19),
            price=Decimal("1.2000"),
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=datetime(2026, 9, 20, tzinfo=UTC),
        )
        session.add(original)
        session.flush()
        error = MarketDataError(
            code="upstream_timeout",
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=NOW,
            attempts=3,
            summary="upstream_timeout: eastmoney request failed after 3 attempts",
        )

        result = FundPriceService(
            session, StubProvider(error=error), clock=lambda: NOW
        ).refresh("000001")

        assert result.status is RefreshStatus.FAILED
        assert result.last_good_price == Decimal("1.20000000")
        assert result.last_good_snapshot is original
        assert result.snapshot is None
        assert result.source == "eastmoney"
        assert result.source_url == SOURCE_URL
        assert result.fetched_at == NOW
        assert result.attempts == 3
        assert result.error_summary == error.summary
        assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1
        session.refresh(original)
        assert original.price == Decimal("1.20000000")
        assert original.error_text is None
        audit = session.scalar(select(AuditEvent))
        assert audit is not None
        assert audit.event_type == "market_price_refresh_failed"
        assert audit.entity_type == "asset"
        assert audit.entity_id == asset.id
        assert json.loads(audit.details_json or "") == {
            "source": "eastmoney",
            "source_url": SOURCE_URL,
            "fetched_at": NOW.isoformat(),
            "attempts": 3,
            "error_code": "upstream_timeout",
            "error_text": error.summary,
        }
    finally:
        session.close()


def test_provider_failure_never_reports_intraday_estimate_as_last_good():
    session, asset = setup_session()
    try:
        session.add(
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=NOW.date(),
                price=Decimal("9.9"),
                source="efinance:estimate",
                source_url="https://example.test/estimate",
                quote_type="intraday_estimate",
                fetched_at=NOW,
            )
        )
        session.flush()
        error = MarketDataError(
            code="upstream_timeout",
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=NOW,
            attempts=3,
            summary="official NAV unavailable",
        )

        result = FundPriceService(
            session, StubProvider(error=error), clock=lambda: NOW
        ).refresh("000001")

        assert result.last_good_snapshot is None
        assert result.last_good_price is None
    finally:
        session.close()


def test_refresh_is_idempotent_for_same_source_and_valuation_date():
    session, _ = setup_session()
    try:
        service = FundPriceService(session, StubProvider(quote()), clock=lambda: NOW)
        first = service.refresh("000001")
        second = service.refresh("000001")

        assert first.snapshot is second.snapshot
        assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1
    finally:
        session.close()


def test_refresh_with_fallback_uses_efinance_after_primary_failure():
    session, _ = setup_session()
    try:
        primary_error = MarketDataError(
            code="upstream_timeout",
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=NOW,
            attempts=3,
            summary="eastmoney unavailable",
        )
        primary = StubProvider(error=primary_error)
        fallback = EfinanceStubProvider(
            quote=FundNavQuote(
                value=Decimal("1.25"),
                valuation_date=date(2026, 9, 22),
                source="efinance",
                source_url=EfinanceStubProvider.source_url,
                fetched_at=NOW,
            )
        )

        result = FundPriceService(
            session, primary, clock=lambda: NOW
        ).refresh_with_fallback("000001", [primary, fallback])

        assert result.status is RefreshStatus.SUCCESS
        assert result.snapshot is not None
        assert result.snapshot.source == "efinance"
        assert primary.calls == ["000001"]
        assert fallback.calls == ["000001"]
        assert [item.source for item in result.provider_attempts] == [
            "eastmoney",
            "efinance",
        ]
        assert result.provider_attempts[0].error_summary == "eastmoney unavailable"
    finally:
        session.close()


def test_refresh_with_fallback_uses_fresh_fallback_after_stale_primary():
    session, _ = setup_session()
    try:
        primary = StubProvider(quote(date(2026, 9, 19)))
        fallback = EfinanceStubProvider(
            quote=FundNavQuote(
                value=Decimal("1.25"),
                valuation_date=date(2026, 9, 22),
                source="efinance",
                source_url=EfinanceStubProvider.source_url,
                fetched_at=NOW,
            )
        )

        result = FundPriceService(
            session, primary, clock=lambda: NOW
        ).refresh_with_fallback("000001", [primary, fallback])

        assert result.status is RefreshStatus.SUCCESS
        assert result.snapshot is not None
        assert result.snapshot.source == "efinance"
        assert primary.calls == ["000001"]
        assert fallback.calls == ["000001"]
        assert [attempt.status for attempt in result.provider_attempts] == [
            RefreshStatus.STALE,
            RefreshStatus.SUCCESS,
        ]
    finally:
        session.close()


def test_refresh_with_fallback_reports_failure_when_all_official_navs_are_stale():
    session, _ = setup_session()
    try:
        primary = StubProvider(quote(date(2026, 9, 18)))
        fallback = EfinanceStubProvider(
            quote=FundNavQuote(
                value=Decimal("1.25"),
                valuation_date=date(2026, 9, 19),
                source="efinance",
                source_url=EfinanceStubProvider.source_url,
                fetched_at=NOW,
            )
        )

        result = FundPriceService(
            session, primary, clock=lambda: NOW
        ).refresh_with_fallback("000001", [primary, fallback])

        assert result.status is RefreshStatus.FAILED
        assert result.is_fresh is False
        assert result.last_good_snapshot is not None
        assert result.last_good_snapshot.source == "efinance"
        assert result.last_good_price == Decimal("1.25000000")
        assert [attempt.status for attempt in result.provider_attempts] == [
            RefreshStatus.STALE,
            RefreshStatus.STALE,
        ]
    finally:
        session.close()


def test_refresh_with_fallback_does_not_accept_intraday_estimate_as_success():
    session, asset = setup_session()
    try:
        existing_official = PriceSnapshot(
            asset_id=asset.id,
            valuation_date=date(2026, 9, 19),
            price=Decimal("1.11"),
            source="eastmoney",
            source_url=SOURCE_URL,
            quote_type="official_nav",
            fetched_at=NOW - timedelta(minutes=5),
        )
        session.add(existing_official)
        session.flush()
        primary = StubProvider(
            FundNavQuote(
                value=Decimal("1.24"),
                valuation_date=date(2026, 9, 19),
                source="eastmoney",
                source_url=SOURCE_URL,
                fetched_at=NOW,
                quote_type=QuoteType.INTRADAY_ESTIMATE,
            )
        )
        fallback = EfinanceStubProvider(
            quote=FundNavQuote(
                value=Decimal("1.25"),
                valuation_date=date(2026, 9, 22),
                source="efinance",
                source_url=EfinanceStubProvider.source_url,
                fetched_at=NOW,
            )
        )

        result = FundPriceService(
            session, primary, clock=lambda: NOW
        ).refresh_with_fallback("000001", [primary, fallback])

        assert result.status is RefreshStatus.SUCCESS
        assert result.snapshot is not None
        assert result.snapshot.source == "efinance"
        assert fallback.calls == ["000001"]
        assert result.provider_attempts[0].status is RefreshStatus.FAILED
        session.refresh(existing_official)
        assert existing_official.price == Decimal("1.11000000")
        assert existing_official.quote_type == "official_nav"
    finally:
        session.close()


def test_estimate_fallback_requires_intraday_quote_type_and_skips_recalculation(
    monkeypatch: pytest.MonkeyPatch,
):
    session, _ = setup_session()
    try:
        official = StubProvider(
            FundNavQuote(
                value=Decimal("1.20"),
                valuation_date=NOW.date(),
                source="eastmoney",
                source_url=SOURCE_URL,
                fetched_at=NOW,
                quote_type=QuoteType.OFFICIAL_NAV,
            )
        )
        estimate = EfinanceStubProvider(
            quote=FundNavQuote(
                value=Decimal("1.23"),
                valuation_date=NOW.date(),
                source="efinance:estimate",
                source_url="https://example.test/estimate",
                fetched_at=NOW,
                quote_type=QuoteType.INTRADAY_ESTIMATE,
            )
        )
        recalculated = False

        def mark_recalculated(*args, **kwargs):
            nonlocal recalculated
            recalculated = True

        monkeypatch.setattr(
            "finance_app.market.service.refresh_current_snapshot_after_price_update",
            mark_recalculated,
        )
        result = FundPriceService(
            session, official, clock=lambda: NOW
        ).refresh_with_fallback(
            "000001",
            [official, estimate],
            required_quote_type=QuoteType.INTRADAY_ESTIMATE,
        )

        assert result.status is RefreshStatus.SUCCESS
        assert result.snapshot is not None
        assert result.snapshot.quote_type == QuoteType.INTRADAY_ESTIMATE
        assert result.snapshot.source == "efinance:estimate"
        assert official.calls == ["000001"]
        assert estimate.calls == ["000001"]
        assert result.provider_attempts[0].status is RefreshStatus.FAILED
        assert recalculated is False
    finally:
        session.close()


def test_failed_estimate_refresh_returns_last_estimate_not_official_nav():
    session, asset = setup_session()
    try:
        official = PriceSnapshot(
            asset_id=asset.id,
            valuation_date=NOW.date(),
            price=Decimal("1.20"),
            source="eastmoney",
            source_url=SOURCE_URL,
            quote_type=QuoteType.OFFICIAL_NAV,
            fetched_at=NOW,
        )
        estimate = PriceSnapshot(
            asset_id=asset.id,
            valuation_date=NOW.date(),
            price=Decimal("1.21"),
            source="tiantian:estimate",
            source_url="https://example.test/estimate",
            quote_type=QuoteType.INTRADAY_ESTIMATE,
            fetched_at=NOW,
        )
        session.add_all((official, estimate))
        session.flush()
        result = FundPriceService(
            session,
            StubProvider(error=RuntimeError("estimate unavailable")),
            clock=lambda: NOW,
        ).refresh(
            "000001",
            required_quote_type=QuoteType.INTRADAY_ESTIMATE,
        )

        assert result.status is RefreshStatus.FAILED
        assert result.last_good_snapshot is estimate
        assert result.last_good_price == Decimal("1.21000000")
    finally:
        session.close()


def test_snapshot_recalculation_error_propagates_without_calling_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    session, _ = setup_session()
    try:
        primary = StubProvider(quote())
        fallback = EfinanceStubProvider(quote=quote())

        def fail_recalculation(*args, **kwargs):
            raise RuntimeError("snapshot recalculation failed")

        monkeypatch.setattr(
            "finance_app.market.service.refresh_current_snapshot_after_price_update",
            fail_recalculation,
        )

        with pytest.raises(RuntimeError, match="snapshot recalculation failed"):
            FundPriceService(
                session, primary, clock=lambda: NOW
            ).refresh_with_fallback("000001", [primary, fallback])

        assert fallback.calls == []
        assert session.in_transaction()
    finally:
        session.close()


def test_refresh_with_fallback_preserves_last_official_nav_when_all_providers_raise():
    session, asset = setup_session()
    try:
        official = PriceSnapshot(
            asset_id=asset.id,
            valuation_date=date(2026, 9, 19),
            price=Decimal("1.20"),
            source="eastmoney",
            source_url=SOURCE_URL,
            quote_type="official_nav",
            fetched_at=NOW,
        )
        session.add(official)
        session.flush()
        primary = StubProvider(error=RuntimeError("primary transport failed"))
        fallback = EfinanceStubProvider(
            error=RuntimeError("fallback response failed")
        )

        result = FundPriceService(
            session, primary, clock=lambda: NOW
        ).refresh_with_fallback("000001", [primary, fallback])

        assert result.status is RefreshStatus.FAILED
        assert result.last_good_snapshot is official
        assert result.last_good_price == Decimal("1.20000000")
        assert [attempt.source for attempt in result.provider_attempts] == [
            "eastmoney",
            "efinance",
        ]
        assert [attempt.error_summary for attempt in result.provider_attempts] == [
            "primary transport failed",
            "fallback response failed",
        ]
        audits = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == "market_price_refresh_failed")
                .order_by(AuditEvent.id)
            )
        )
        assert [json.loads(audit.details_json or "")["source"] for audit in audits] == [
            "eastmoney",
            "efinance",
        ]
        assert all(
            json.loads(audit.details_json or "")["error_code"]
            == "provider_exception"
            for audit in audits
        )
    finally:
        session.close()


def test_last_good_prefers_manual_nav_over_newer_same_day_automatic_sources():
    session, asset = setup_session()
    try:
        session.add_all(
            [
                PriceSnapshot(
                    asset_id=asset.id,
                    valuation_date=date(2026, 9, 22),
                    price=Decimal("1.10"),
                    source="eastmoney",
                    fetched_at=NOW - timedelta(minutes=1),
                ),
                PriceSnapshot(
                    asset_id=asset.id,
                    valuation_date=date(2026, 9, 22),
                    price=Decimal("1.20"),
                    source="manual:user-entry",
                    fetched_at=NOW - timedelta(minutes=5),
                ),
            ]
        )
        session.flush()
        error = MarketDataError(
            code="upstream_timeout",
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=NOW,
            attempts=3,
            summary="official NAV unavailable",
        )

        result = FundPriceService(
            session, StubProvider(error=error), clock=lambda: NOW
        ).refresh("000001")

        assert result.last_good_snapshot is not None
        assert result.last_good_snapshot.source == "manual:user-entry"
        assert result.last_good_price == Decimal("1.20000000")
    finally:
        session.close()


def test_official_refresh_revalues_existing_current_snapshot():
    session, asset = setup_session()
    try:
        account = Account(name="cash", kind="cash", opening_balance_cents=1000)
        session.add(account)
        session.flush()
        session.add(
            Holding(
                account_id=account.id,
                asset_id=asset.id,
                quantity=Decimal(10),
                cost_cents=1000,
            )
        )
        create_daily_snapshot(
            session, now=NOW, holdings_confirmed_at=NOW, cash_confirmed_at=NOW
        )
        session.flush()
        assert session.scalar(select(PortfolioSnapshot)).total_value_cents is None

        result = FundPriceService(
            session,
            StubProvider(quote(valuation_date=NOW.date())),
            clock=lambda: NOW,
        ).refresh("000001")

        assert result.status is RefreshStatus.SUCCESS
        snapshot = session.scalar(select(PortfolioSnapshot))
        assert snapshot is not None
        assert snapshot.total_value_cents == 2235
        assert snapshot.data_complete is True
    finally:
        session.close()


def test_valid_eight_decimal_nav_survives_commit_expire_and_new_session():
    session, asset = setup_session()
    engine = session.get_bind()
    try:
        exact_quote = FundNavQuote(
            value=Decimal("1000000.00000000"),
            valuation_date=date(2026, 9, 22),
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=NOW,
        )
        result = FundPriceService(
            session, StubProvider(exact_quote), clock=lambda: NOW
        ).refresh("000001")
        assert result.status is RefreshStatus.SUCCESS
        session.commit()
        session.expire_all()
        assert session.scalar(select(PriceSnapshot.price)) == Decimal(
            "1000000.00000000"
        )
        asset_id = asset.id
    finally:
        session.close()

    with Session(engine) as reloaded:
        snapshot = reloaded.scalar(
            select(PriceSnapshot).where(PriceSnapshot.asset_id == asset_id)
        )
        assert snapshot is not None
        assert snapshot.price == Decimal("1000000.00000000")


@pytest.mark.parametrize(
    "raw_nav",
    [
        "0.000000001",
        "1000000.00000001",
        "9999999999999999.99999999",
    ],
)
def test_sqlite_unsafe_nav_fails_without_replacing_last_good(raw_nav: str):
    session, asset = setup_session()
    engine = session.get_bind()
    try:
        original = PriceSnapshot(
            asset_id=asset.id,
            valuation_date=date(2026, 9, 19),
            price=Decimal("1.20000000"),
            source="eastmoney",
            source_url=SOURCE_URL,
            fetched_at=datetime(2026, 9, 20, tzinfo=UTC),
        )
        session.add(original)
        session.flush()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": raw_nav}]
                    }
                },
            )

        provider = EastMoneyFundNavProvider(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: NOW,
        )
        result = FundPriceService(session, provider, clock=lambda: NOW).refresh(
            "000001"
        )

        assert result.status is RefreshStatus.FAILED
        assert result.last_good_price == Decimal("1.20000000")
        assert result.snapshot is None
        assert result.error_summary is not None
        assert "invalid_response" in result.error_summary
        assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 1
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "market_price_refresh_failed"
            )
        )
        assert audit is not None
        assert json.loads(audit.details_json or "")["error_code"] == "invalid_response"
        session.commit()
        asset_id = asset.id
    finally:
        session.close()

    with Session(engine) as reloaded:
        persisted = reloaded.scalar(
            select(PriceSnapshot).where(PriceSnapshot.asset_id == asset_id)
        )
        assert persisted is not None
        assert persisted.price == Decimal("1.20000000")


def test_non_fund_asset_is_not_fetched_or_modified():
    session, asset = setup_session()
    try:
        asset.asset_class = "cash"
        provider = StubProvider(quote())
        result = FundPriceService(session, provider, clock=lambda: NOW).refresh("000001")

        assert result.status is RefreshStatus.FAILED
        assert result.error_summary == "asset_not_found: fund asset 000001 was not found"
        assert provider.calls == []
        assert session.scalar(select(PriceSnapshot)) is None
    finally:
        session.close()


def test_refresh_uses_the_cn_asset_when_another_market_has_the_same_code():
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        non_cn = Asset(
            code="000001", market="HK", name="HK Asset", asset_class="fund"
        )
        cn = Asset(code="000001", market="CN", name="CN Fund", asset_class="fund")
        session.add_all((non_cn, cn))
        session.flush()

        result = FundPriceService(
            session, StubProvider(quote()), clock=lambda: NOW
        ).refresh("000001")

        assert result.status is RefreshStatus.SUCCESS
        assert result.snapshot is not None
        assert result.snapshot.asset_id == cn.id


def test_official_and_intraday_snapshots_with_same_source_and_date_are_isolated():
    session, asset = setup_session()
    try:
        official = quote()
        intraday = FundNavQuote(
            value=Decimal("1.2500"),
            valuation_date=official.valuation_date,
            source=official.source,
            source_url=official.source_url,
            fetched_at=NOW,
            quote_type=QuoteType.INTRADAY_ESTIMATE,
        )
        official_result = FundPriceService(
            session, StubProvider(official), clock=lambda: NOW
        ).refresh("000001", required_quote_type=QuoteType.OFFICIAL_NAV)
        intraday_result = FundPriceService(
            session, StubProvider(intraday), clock=lambda: NOW
        ).refresh("000001", required_quote_type=QuoteType.INTRADAY_ESTIMATE)

        assert official_result.snapshot is not None
        assert intraday_result.snapshot is not None
        rows = list(
            session.scalars(
                select(PriceSnapshot).where(PriceSnapshot.asset_id == asset.id)
            )
        )
        assert {row.quote_type for row in rows} == {
            QuoteType.OFFICIAL_NAV.value,
            QuoteType.INTRADAY_ESTIMATE.value,
        }
        official_row = next(
            row for row in rows if row.quote_type == QuoteType.OFFICIAL_NAV.value
        )
        assert official_row.price == Decimal("1.23450000")
    finally:
        session.close()


def test_non_cn_fund_is_not_sent_to_the_cn_only_provider():
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Asset(code="000001", market="HK", name="HK Asset", asset_class="fund")
        )
        session.flush()
        provider = StubProvider(quote())

        result = FundPriceService(session, provider, clock=lambda: NOW).refresh(
            "000001"
        )

        assert result.status is RefreshStatus.FAILED
        assert provider.calls == []
        assert session.scalar(select(PriceSnapshot)) is None
