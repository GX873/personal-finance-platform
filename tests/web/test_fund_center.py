from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from finance_app.db import Base, create_db_engine
from finance_app.ledger.models import Account, Asset, Holding
from finance_app.portfolio.models import PriceSnapshot
from finance_app.web.fund_center import build_fund_center

NOW = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)


@pytest.fixture
def db_session() -> Session:
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@dataclass(frozen=True)
class SeededCenter:
    selected_asset_id: int
    held_asset_ids: list[int]


def add_price(
    db: Session,
    asset: Asset,
    *,
    value: str,
    valuation_date: date,
    source: str = "eastmoney",
    quote_type: str = "official_nav",
    fetched_at: datetime | None = None,
    error_text: str | None = None,
) -> PriceSnapshot:
    row = PriceSnapshot(
        asset_id=asset.id,
        valuation_date=valuation_date,
        source=source,
        price=Decimal(value),
        quote_type=quote_type,
        source_url=f"https://example.test/{asset.code}",
        fetched_at=fetched_at or NOW - timedelta(minutes=5),
        error_text=error_text,
    )
    db.add(row)
    return row


def seed_fund_center(db: Session) -> SeededCenter:
    first_account = Account(name="基金账户一", kind="investment", currency="CNY")
    second_account = Account(name="基金账户二", kind="investment", currency="CNY")
    selected = Asset(
        code="000001",
        market="CN",
        name="核心基金",
        asset_class="fund",
        currency="CNY",
    )
    smaller = Asset(
        code="000002",
        market="CN",
        name="卫星基金",
        asset_class="fund",
        currency="CNY",
    )
    cleared = Asset(
        code="000003",
        market="CN",
        name="已清仓基金",
        asset_class="fund",
        currency="CNY",
    )
    hk_fund = Asset(
        code="HK0001",
        market="HK",
        name="境外基金",
        asset_class="fund",
        currency="CNY",
    )
    stock = Asset(
        code="600001",
        market="CN",
        name="股票",
        asset_class="stock",
        currency="CNY",
    )
    db.add_all(
        [first_account, second_account, selected, smaller, cleared, hk_fund, stock]
    )
    db.flush()
    db.add_all(
        [
            Holding(
                account_id=first_account.id,
                asset_id=selected.id,
                quantity=Decimal(4),
                cost_cents=400,
            ),
            Holding(
                account_id=second_account.id,
                asset_id=selected.id,
                quantity=Decimal(6),
                cost_cents=500,
            ),
            Holding(
                account_id=first_account.id,
                asset_id=smaller.id,
                quantity=Decimal(2),
                cost_cents=180,
            ),
            Holding(
                account_id=first_account.id,
                asset_id=cleared.id,
                quantity=Decimal(0),
                cost_cents=0,
            ),
            Holding(
                account_id=first_account.id,
                asset_id=hk_fund.id,
                quantity=Decimal(10),
                cost_cents=1_000,
            ),
            Holding(
                account_id=first_account.id,
                asset_id=stock.id,
                quantity=Decimal(10),
                cost_cents=1_000,
            ),
        ]
    )
    add_price(
        db,
        selected,
        value="1.00000000",
        valuation_date=date(2025, 10, 1),
    )
    add_price(
        db,
        selected,
        value="1.20000000",
        valuation_date=date(2026, 9, 29),
    )
    add_price(
        db,
        selected,
        value="1.23000000",
        valuation_date=date(2026, 9, 30),
        source="tiantian:estimate",
        quote_type="intraday_estimate",
    )
    add_price(
        db,
        smaller,
        value="1.00000000",
        valuation_date=date(2026, 9, 29),
    )
    db.commit()
    return SeededCenter(selected.id, [selected.id, smaller.id])


def seed_holding_without_prices(db: Session) -> Asset:
    account = Account(name="基金账户", kind="investment", currency="CNY")
    asset = Asset(
        code="000010",
        market="CN",
        name="暂无净值基金",
        asset_class="fund",
        currency="CNY",
    )
    db.add_all([account, asset])
    db.flush()
    db.add(
        Holding(
            account_id=account.id,
            asset_id=asset.id,
            quantity=Decimal(10),
            cost_cents=1_000,
        )
    )
    db.commit()
    return asset


def seed_history_with_large_intraday_outlier(db: Session) -> Asset:
    asset = seed_holding_without_prices(db)
    add_price(
        db,
        asset,
        value="0.80",
        valuation_date=date(2026, 8, 29),
        source="efinance",
    )
    add_price(db, asset, value="1.00", valuation_date=date(2026, 8, 30))
    add_price(db, asset, value="1.10", valuation_date=date(2026, 9, 29))
    add_price(
        db,
        asset,
        value="99.00",
        valuation_date=date(2026, 9, 30),
        source="tiantian:estimate",
        quote_type="intraday_estimate",
    )
    db.commit()
    return asset


def test_fund_center_uses_official_nav_for_value_and_estimate_only_for_preview(
    db_session: Session,
):
    seeded = seed_fund_center(db_session)

    vm = build_fund_center(
        db_session,
        selected_asset_id=seeded.selected_asset_id,
        period="1y",
        now=NOW,
    )

    assert [row["asset_id"] for row in vm["funds"]] == seeded.held_asset_ids
    assert vm["selected"]["quantity"] == Decimal("10.00000000")
    assert vm["selected"]["cost_cents"] == 900
    assert vm["selected"]["official_nav"] == "1.20000000"
    assert vm["selected"]["intraday_estimate"] == "1.23000000"
    assert vm["selected"]["official_value_cents"] == 1200
    assert vm["selected"]["estimated_value_cents"] == 1230
    assert vm["selected"]["official_profit_cents"] == 300
    assert vm["selected"]["estimated_profit_cents"] == 330
    assert vm["selected"]["data_source"] == "eastmoney"
    assert vm["selected"]["estimate_source"] == "tiantian:estimate"
    assert vm["metrics"]["total_return"] is not None
    assert vm["funds"][0]["portfolio_percent"] == Decimal("0.85714286")


def test_fund_center_never_substitutes_missing_values_with_zero(db_session: Session):
    asset = seed_holding_without_prices(db_session)
    account_id = db_session.query(Holding.account_id).scalar()
    priced_asset = Asset(
        code="000011",
        market="CN",
        name="已有净值基金",
        asset_class="fund",
        currency="CNY",
    )
    db_session.add(priced_asset)
    db_session.flush()
    db_session.add(
        Holding(
            account_id=account_id,
            asset_id=priced_asset.id,
            quantity=Decimal(10),
            cost_cents=1_000,
        )
    )
    add_price(
        db_session,
        priced_asset,
        value="1.10",
        valuation_date=date(2026, 9, 29),
    )
    db_session.commit()

    vm = build_fund_center(
        db_session, selected_asset_id=asset.id, period="1y", now=NOW
    )

    assert vm["selected"]["official_nav"] is None
    assert vm["selected"]["official_value_cents"] is None
    assert vm["selected"]["intraday_estimate"] is None
    assert vm["selected"]["estimated_value_cents"] is None
    assert vm["selected"]["holding_return"] is None
    assert vm["metrics"]["total_return"] is None
    assert vm["chart"]["points"] == []
    assert all(row["portfolio_percent"] is None for row in vm["funds"])


def test_history_and_metrics_ignore_intraday_rows_and_apply_period_cutoff(
    db_session: Session,
):
    asset = seed_history_with_large_intraday_outlier(db_session)

    vm = build_fund_center(
        db_session, selected_asset_id=asset.id, period="1m", now=NOW
    )

    assert [point["valuation_date"] for point in vm["history"]] == [
        "2026-08-30",
        "2026-09-29",
    ]
    assert all(point["quote_type"] == "official_nav" for point in vm["history"])
    assert vm["metrics"]["total_return"] == Decimal("0.1")
    assert vm["chart"]["view_box"] == "0 0 600 220"
    assert len(vm["chart"]["points"]) == 2
    assert all(20 <= point["x"] <= 580 for point in vm["chart"]["points"])
    assert all(20 <= point["y"] <= 190 for point in vm["chart"]["points"])


def test_latest_quotes_reject_unusable_rows_and_honor_source_priority(
    db_session: Session,
):
    asset = seed_holding_without_prices(db_session)
    add_price(
        db_session,
        asset,
        value="1.10",
        valuation_date=date(2026, 9, 29),
        source="efinance",
    )
    add_price(
        db_session,
        asset,
        value="1.20",
        valuation_date=date(2026, 9, 29),
        source="manual:admin",
    )
    add_price(
        db_session,
        asset,
        value="9.00",
        valuation_date=date(2026, 9, 30),
        source="future",
        fetched_at=NOW + timedelta(seconds=1),
    )
    add_price(
        db_session,
        asset,
        value="8.00",
        valuation_date=date(2026, 9, 30),
        source="broken",
        error_text="upstream failed",
    )
    add_price(
        db_session,
        asset,
        value="7.00",
        valuation_date=date(2026, 9, 30),
        source="   ",
    )
    db_session.commit()

    vm = build_fund_center(
        db_session, selected_asset_id=asset.id, period="invalid", now=NOW
    )

    assert vm["period"] == "1y"
    assert vm["selected"]["official_nav"] == "1.20000000"
    assert vm["selected"]["data_source"] == "manual:admin"
    assert [point["source"] for point in vm["history"]] == ["manual:admin"]


def test_unknown_selection_falls_back_to_first_held_fund_and_empty_is_stable(
    db_session: Session,
):
    seeded = seed_fund_center(db_session)
    vm = build_fund_center(
        db_session, selected_asset_id=999_999, period="all", now=NOW
    )
    assert vm["selected"]["asset_id"] == seeded.selected_asset_id

    for holding in db_session.query(Holding):
        holding.quantity = Decimal(0)
    db_session.commit()
    empty = build_fund_center(
        db_session, selected_asset_id=None, period="all", now=NOW
    )
    assert empty["funds"] == []
    assert empty["selected"]["asset_id"] is None
    assert empty["history"] == []
    assert empty["metrics"]["total_return"] is None
    assert empty["chart"] == {
        "view_box": "0 0 600 220",
        "points": [],
        "polyline": "",
    }
