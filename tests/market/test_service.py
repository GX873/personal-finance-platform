from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_app.db import Base, create_db_engine
from finance_app.ledger.models import Asset, AuditEvent
from finance_app.market.base import FundNavQuote, MarketDataError
from finance_app.market.service import FundPriceService, RefreshStatus
from finance_app.portfolio.models import PriceSnapshot

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
