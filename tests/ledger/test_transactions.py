from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from finance_app.ledger.models import Account, Asset, AuditEvent, Holding, Transaction
from finance_app.ledger.schemas import PostTransaction
from finance_app.ledger.service import (
    TransactionConflict,
    account_cash_balance,
    calculate_position,
    post_transaction,
    rebuild_position,
    reverse_transaction,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def funded(session):
    account = Account(name="Broker", kind="investment")
    asset = Asset(code="TEST", name="Test", market="CN")
    session.add_all([account, asset])
    session.commit()
    post_transaction(
        session, PostTransaction("test", "fund", "TRANSFER_IN", account.id, 100000, NOW)
    )
    session.commit()
    return account.id, asset.id


def buy(ids, key="buy", **changes):
    return replace(
        PostTransaction(
            "test",
            key,
            "BUY",
            ids[0],
            10000,
            NOW + timedelta(days=1),
            asset_id=ids[1],
            quantity=Decimal(10),
            fee_cents=3,
        ),
        **changes,
    )


def test_cost_cash_sales_and_rebuild(session, funded):
    post_transaction(session, buy(funded))
    sale = buy(
        funded,
        "sell",
        kind="SELL",
        quantity=Decimal(3),
        amount_cents=5000,
        fee_cents=5,
        occurred_at=NOW + timedelta(days=2),
    )
    post_transaction(session, sale)
    assert calculate_position(session, *funded) == (Decimal(7), 7002)
    assert account_cash_balance(session, funded[0]) == 94992
    holding = rebuild_position(session, *funded)
    assert (holding.quantity, holding.cost_cents) == (Decimal(7), 7002)
    holding.valuation_cents = 20000
    post_transaction(session, replace(sale, external_id="exit", quantity=Decimal(7)))
    assert calculate_position(session, *funded) == (Decimal(0), 0)
    assert holding.valuation_cents is None


def test_extreme_cost_partial_sale_rounds_without_intermediate_precision_loss(
    session, funded
):
    cost = 9000049999999910000
    post_transaction(
        session,
        PostTransaction("test", "large-funding", "TRANSFER_IN", funded[0], cost, NOW),
    )
    post_transaction(
        session,
        buy(
            funded, amount_cents=cost, fee_cents=0, quantity=Decimal("999999.99999999")
        ),
    )
    post_transaction(
        session,
        buy(
            funded,
            "partial",
            kind="SELL",
            amount_cents=1,
            fee_cents=0,
            quantity=Decimal("999999.99999998"),
            occurred_at=NOW + timedelta(days=2),
        ),
    )
    session.commit()
    session.expire_all()
    position = calculate_position(session, *funded)
    assert position.quantity == Decimal("0.00000001")
    assert position.cost_cents == 90001
    assert rebuild_position(session, *funded).cost_cents == 90001


def test_idempotency_conflict_and_rollback(session, funded):
    command = buy(funded)
    original = post_transaction(session, command)
    assert post_transaction(session, command).id == original.id
    with pytest.raises(TransactionConflict):
        post_transaction(session, replace(command, amount_cents=9999))
    session.rollback()
    assert calculate_position(session, *funded) == (Decimal(0), 0)
    assert len(session.scalars(select(Transaction)).all()) == 1


def test_reversal_rebuilds_basis_and_rejects_invalid_history(session, funded):
    original = post_transaction(session, buy(funded))
    sale = post_transaction(
        session,
        buy(
            funded,
            "sale",
            kind="SELL",
            quantity=Decimal(3),
            occurred_at=NOW + timedelta(days=2),
        ),
    )
    with pytest.raises(ValueError, match="position"):
        reverse_transaction(
            session, original.id, "mistake", source="test", external_id="r-buy"
        )
    reversal = reverse_transaction(
        session, sale.id, "mistake", source="test", external_id="r-sale"
    )
    assert reversal.reverses_transaction_id == sale.id
    assert calculate_position(session, *funded) == (Decimal(10), 10003)
    with pytest.raises(ValueError):
        reverse_transaction(
            session, sale.id, "mistake", source="test", external_id="twice"
        )
    with pytest.raises(ValueError):
        reverse_transaction(
            session,
            reversal.id,
            "mistake",
            source="test",
            external_id="reverse-reverse",
        )
    assert len(session.scalars(select(AuditEvent)).all()) == 4


def test_oversell_backdate_and_cash_shortfall_are_atomic(session, funded):
    for command in [
        buy(funded, kind="SELL"),
        buy(funded, amount_cents=100001),
        buy(funded, occurred_at=NOW - timedelta(days=1)),
    ]:
        with pytest.raises(ValueError):
            post_transaction(session, command)
    assert len(session.scalars(select(Transaction)).all()) == 1
    assert session.scalars(select(Holding)).all() == []
    post_transaction(session, buy(funded))


def test_failure_after_insert_rolls_back_transaction_and_cache(session, funded):
    session.execute(
        text(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    )
    session.commit()
    with pytest.raises(IntegrityError):
        post_transaction(session, buy(funded))
    assert session.scalars(select(Holding)).all() == []
    assert len(session.scalars(select(Transaction)).all()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_cents": -1},
        {"amount_cents": True},
        {"amount_cents": float("nan")},
        {"fee_cents": -1},
        {"quantity": Decimal("NaN")},
        {"quantity": Decimal("Infinity")},
        {"quantity": Decimal("0.000000001")},
        {"quantity": Decimal(1000001)},
        {"occurred_at": NOW.replace(tzinfo=None)},
        {"quantity": Decimal(0)},
        {"kind": "TRANSFER_IN"},
        {"account_id": 999},
        {"asset_id": 999},
    ],
)
def test_invalid_input(session, funded, changes):
    with pytest.raises(ValueError):
        post_transaction(session, buy(funded, **changes))


def test_decimal_roundtrip_and_fee_dividend_cash(session, funded):
    command = buy(
        funded, quantity=Decimal("999999.12345678"), price=Decimal("999999.87654321")
    )
    row = post_transaction(session, command)
    session.commit()
    session.expire_all()
    assert row.quantity == command.quantity
    assert row.price == command.price
    assert post_transaction(session, command).id == row.id
    for key, kind, amount, fee in [
        ("div", "DIVIDEND", 500, 2),
        ("fee", "FEE", 10, 0),
        ("out", "TRANSFER_OUT", 100, 1),
    ]:
        post_transaction(
            session,
            PostTransaction(
                "test",
                key,
                kind,
                funded[0],
                amount,
                NOW + timedelta(days=3),
                fee_cents=fee,
            ),
        )
    assert account_cash_balance(session, funded[0]) == 90384
