"""Append-only transactions, historical replay, and derived holding caches."""

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.db import utc_now
from finance_app.ledger.models import Account, Asset, AuditEvent, Holding, Transaction
from finance_app.ledger.schemas import PostTransaction

KINDS = {"BUY", "SELL", "DIVIDEND", "TRANSFER_IN", "TRANSFER_OUT", "FEE"}
MAX_CENTS = 2**63 - 1


class TransactionConflict(ValueError):
    """An external key already refers to a different immutable transaction."""


class Position(NamedTuple):
    quantity: Decimal
    cost_cents: int


def _decimal(value: Decimal) -> None:
    # SQLite NUMERIC uses doubles. Restrict both scale and magnitude so all
    # accepted decimals round-trip to eight places without losing a unit.
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
        or value > 1000000
        or int(value.as_tuple().exponent) < -8
    ):
        raise ValueError(
            "decimal must be finite, nonnegative, <= 1000000 with <= 8 places"
        )


def _validate(command: PostTransaction) -> None:
    if command.kind not in KINDS:
        raise ValueError("unsupported transaction kind")
    for value, limit in [(command.source, 64), (command.external_id, 128)]:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("source and external_id must be nonempty bounded strings")
    for cents in [command.amount_cents, command.fee_cents]:
        if type(cents) is not int or not 0 <= cents <= MAX_CENTS:
            raise ValueError("money must be nonnegative integer cents")
    if command.amount_cents == 0:
        raise ValueError("amount must be positive")
    if type(command.account_id) is not int or command.account_id <= 0:
        raise ValueError("invalid account id")
    if command.asset_id is not None and (
        type(command.asset_id) is not int or command.asset_id <= 0
    ):
        raise ValueError("invalid asset id")
    if (
        not isinstance(command.occurred_at, datetime)
        or command.occurred_at.utcoffset() is None
    ):
        raise ValueError("occurred_at must be timezone-aware")
    _decimal(command.quantity)
    if command.price is not None:
        _decimal(command.price)
    if command.kind in {"BUY", "SELL"}:
        if command.asset_id is None or command.quantity <= 0:
            raise ValueError("buy/sell requires asset and positive quantity")
    elif command.quantity != 0 or command.price is not None:
        raise ValueError("cash transactions cannot have quantity or price")
    if (
        command.kind in {"TRANSFER_IN", "TRANSFER_OUT", "FEE"}
        and command.asset_id is not None
    ):
        raise ValueError("cash transfer/fee cannot have an asset")
    if command.note is not None and not isinstance(command.note, str):
        raise ValueError("note must be text")


def _begin(session: Session) -> None:
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")


def _rows(session: Session, account_id: int) -> list[Transaction]:
    rows = list(
        session.scalars(
            select(Transaction)
            .where(Transaction.account_id == account_id)
            .order_by(Transaction.occurred_at, Transaction.id)
        )
    )
    reversed_ids = {
        row.reverses_transaction_id
        for row in rows
        if row.reverses_transaction_id is not None
    }
    # Reversals cancel the original event for replay; replay every remaining
    # historical prefix, so cancellation cannot hide a later historical oversell.
    return [
        row
        for row in rows
        if row.reverses_transaction_id is None and row.id not in reversed_ids
    ]


def _replay(
    session: Session, account_id: int
) -> tuple[int, dict[int, tuple[Decimal, int]]]:
    account = session.get(Account, account_id)
    if account is None:
        raise ValueError("account does not exist")
    cash = account.opening_balance_cents
    positions: dict[int, tuple[Decimal, int]] = {}
    for row in _rows(session, account_id):
        if row.kind in {"BUY", "SELL"}:
            assert row.asset_id is not None
            quantity, cost = positions.get(row.asset_id, (Decimal(0), 0))
            if row.kind == "BUY":
                quantity += row.quantity
                _decimal(quantity)
                cost += row.amount_cents + row.fee_cents
                cash -= row.amount_cents + row.fee_cents
            else:
                if row.quantity > quantity:
                    raise ValueError(
                        "transaction would make historical position negative"
                    )
                sold_numerator, sold_denominator = row.quantity.as_integer_ratio()
                total_numerator, total_denominator = quantity.as_integer_ratio()
                numerator = cost * sold_numerator * total_denominator
                denominator = sold_denominator * total_numerator
                # Exact positive rational HALF_UP avoids Decimal context rounding
                # a large cost before the final cent is rounded.
                removed_cost = (2 * numerator + denominator) // (2 * denominator)
                quantity -= row.quantity
                cost = cost - removed_cost if quantity else 0
                cash += row.amount_cents - row.fee_cents
            if cost > MAX_CENTS:
                raise ValueError("position cost exceeds integer range")
            positions[row.asset_id] = (quantity, cost)
        elif row.kind in {"TRANSFER_IN", "DIVIDEND"}:
            cash += row.amount_cents - row.fee_cents
        elif row.kind in {"TRANSFER_OUT", "FEE"}:
            cash -= row.amount_cents + row.fee_cents
        else:
            raise ValueError("unsupported stored transaction kind")
        if not 0 <= cash <= MAX_CENTS:
            raise ValueError("insufficient historical cash or balance out of range")
    return cash, positions


def account_cash_balance(session: Session, account_id: int) -> int:
    return _replay(session, account_id)[0]


def calculate_position(session: Session, account_id: int, asset_id: int) -> Position:
    return Position(*_replay(session, account_id)[1].get(asset_id, (Decimal(0), 0)))


def rebuild_position(session: Session, account_id: int, asset_id: int) -> Holding:
    quantity, cost = calculate_position(session, account_id, asset_id)
    if session.get(Asset, asset_id) is None:
        raise ValueError("asset does not exist")
    holding = session.scalar(
        select(Holding).where(
            Holding.account_id == account_id, Holding.asset_id == asset_id
        )
    )
    if holding is None:
        holding = Holding(account_id=account_id, asset_id=asset_id)
        session.add(holding)
    if holding.quantity != quantity:
        holding.valuation_cents = None
        holding.is_price_stale = True
    holding.quantity, holding.cost_cents = quantity, cost
    session.flush()
    return holding


def _payload(command: PostTransaction) -> dict:
    return {
        "source": command.source,
        "external_id": command.external_id,
        "kind": command.kind,
        "account_id": command.account_id,
        "asset_id": command.asset_id,
        "amount_cents": command.amount_cents,
        "quantity": command.quantity,
        "price": command.price,
        "fee_cents": command.fee_cents,
        "occurred_at": command.occurred_at,
        "description": command.note,
    }


def _finish(session: Session, row: Transaction, event: str) -> None:
    session.add(row)
    session.flush()
    _replay(session, row.account_id)
    if row.asset_id is not None:
        rebuild_position(session, row.account_id, row.asset_id)
    session.add(
        AuditEvent(
            event_type=event,
            entity_type="transaction",
            entity_id=row.id,
            details_json=json.dumps(
                {
                    "note": row.description,
                    "reverses_transaction_id": row.reverses_transaction_id,
                }
            ),
        )
    )
    session.flush()


def post_transaction(session: Session, command: PostTransaction) -> Transaction:
    """Flush an atomic posting; the caller retains ownership of commit/rollback."""
    if not isinstance(command.kind, str):
        raise ValueError("unsupported transaction kind")  # noqa: TRY004
    command = replace(command, kind=command.kind.upper())
    _validate(command)
    _begin(session)
    with session.begin_nested():
        existing = session.scalar(
            select(Transaction).where(
                Transaction.source == command.source,
                Transaction.external_id == command.external_id,
            )
        )
        payload = _payload(command)
        if existing is not None:
            if existing.reverses_transaction_id is not None or any(
                getattr(existing, key) != value for key, value in payload.items()
            ):
                raise TransactionConflict("external key has different payload")
            return existing
        if session.get(Account, command.account_id) is None:
            raise ValueError("account does not exist")
        if (
            command.asset_id is not None
            and session.get(Asset, command.asset_id) is None
        ):
            raise ValueError("asset does not exist")
        row = Transaction(**payload)
        _finish(session, row, "transaction.posted")
        return row


def reverse_transaction(
    session: Session,
    transaction_id: int,
    reason: str,
    *,
    source: str = "reversal",
    external_id: str | None = None,
    occurred_at: datetime | None = None,
    note: str | None = None,
) -> Transaction:
    """Append a cancellation, retaining the original and validating replay."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reversal reason is required")
    external_id = external_id or str(transaction_id)
    _begin(session)
    with session.begin_nested():
        original = session.get(Transaction, transaction_id)
        if original is None or original.reverses_transaction_id is not None:
            raise ValueError("original transaction does not exist or is a reversal")
        if (
            session.scalar(
                select(Transaction.id).where(
                    Transaction.reverses_transaction_id == transaction_id
                )
            )
            is not None
        ):
            raise ValueError("transaction is already reversed")
        command = PostTransaction(
            source,
            external_id,
            original.kind,
            original.account_id,
            original.amount_cents,
            occurred_at or utc_now(),
            original.asset_id,
            original.quantity,
            original.price,
            original.fee_cents,
            reason if note is None else f"{reason}: {note}",
        )
        _validate(command)
        if command.occurred_at < original.occurred_at:
            raise ValueError("reversal cannot precede original")
        if (
            session.scalar(
                select(Transaction.id).where(
                    Transaction.source == source, Transaction.external_id == external_id
                )
            )
            is not None
        ):
            raise TransactionConflict("external key already used")
        row = Transaction(**_payload(command), reverses_transaction_id=original.id)
        _finish(session, row, "transaction.reversed")
        return row
