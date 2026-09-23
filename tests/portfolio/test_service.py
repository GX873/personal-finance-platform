import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from sqlalchemy.orm import Session

from finance_app.db import Base, create_db_engine
from finance_app.ledger.models import Account, Asset, Holding
from finance_app.portfolio.models import PriceSnapshot
from finance_app.portfolio.service import create_daily_snapshot, value_cents

NOW = datetime(2026, 9, 23, 14, tzinfo=UTC)


def test_exact_cent_rounding_independent_of_context():
    with localcontext() as ctx:
        ctx.prec = 3
        assert (
            value_cents(Decimal("123456.12345678"), Decimal("123.45678901"))
            == 1524149659
        )
        assert value_cents(Decimal(1), Decimal("1.005")) == 101


def test_snapshot_unknown_price_partial_then_fresh_and_idempotent():
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = Account(name="cash", kind="investment", opening_balance_cents=1000)
        asset = Asset(code="1", market="CN", name="fund")
        session.add_all([account, asset])
        session.flush()
        session.add(
            Holding(
                account_id=account.id,
                asset_id=asset.id,
                quantity=Decimal(10),
                cost_cents=1000,
            )
        )
        session.flush()
        row = create_daily_snapshot(
            session, now=NOW, holdings_confirmed_at=NOW, cash_confirmed_at=NOW
        )
        assert row.total_value_cents is None and not row.data_complete
        assert row.known_value_cents == 1000
        session.add(
            PriceSnapshot(
                asset_id=asset.id,
                valuation_date=NOW.date(),
                price=Decimal("1.005"),
                source="manual",
                fetched_at=NOW,
            )
        )
        session.flush()
        row2 = create_daily_snapshot(
            session, now=NOW, holdings_confirmed_at=NOW, cash_confirmed_at=NOW
        )
        assert row2.id == row.id
        assert row2.total_value_cents == 2005 and row2.data_complete
        details = json.loads(row2.details_json)
        assert details["holdings"][0]["source"] == "manual"
        assert details["holdings"][0]["valuation_date"] == "2026-09-23"
        row3 = create_daily_snapshot(session, now=NOW)
        assert row3.total_value_cents is None
        assert not json.loads(row3.details_json)["holdings_fresh"]
        session.rollback()
    with Session(engine) as session:
        assert session.query(Account).count() == 0


def test_stale_and_future_price_do_not_become_complete():
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        account = Account(name="a", kind="cash")
        asset = Asset(code="2", market="CN", name="fund")
        session.add_all([account, asset])
        session.flush()
        session.add(
            Holding(account_id=account.id, asset_id=asset.id, quantity=Decimal(1))
        )
        session.add_all(
            [
                PriceSnapshot(
                    asset_id=asset.id,
                    valuation_date=date(2026, 9, 22),
                    price=Decimal(2),
                    source="manual",
                    fetched_at=NOW,
                ),
                PriceSnapshot(
                    asset_id=asset.id,
                    valuation_date=date(2026, 9, 24),
                    price=Decimal(99),
                    source="manual",
                    fetched_at=NOW,
                ),
            ]
        )
        session.flush()
        row = create_daily_snapshot(
            session, now=NOW, holdings_confirmed_at=NOW, cash_confirmed_at=NOW
        )
        assert row.total_value_cents is None and row.known_value_cents == 200


def test_snapshot_rejects_future_confirmation_and_backward_time():
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="future"):
            create_daily_snapshot(
                session, now=NOW, holdings_confirmed_at=NOW + timedelta(seconds=1)
            )
        create_daily_snapshot(session, now=NOW)
        with pytest.raises(ValueError, match="backwards"):
            create_daily_snapshot(session, now=NOW - timedelta(seconds=1))


@pytest.mark.parametrize(
    "quantity,price",
    [
        (Decimal("NaN"), Decimal(1)),
        (Decimal(1), Decimal(-1)),
        (Decimal("1e30"), Decimal(1)),
    ],
)
def test_valuation_rejects_invalid_and_overflow(quantity, price):
    with pytest.raises(ValueError):
        value_cents(quantity, price)
