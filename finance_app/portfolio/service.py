"""Local valuation and daily snapshots; caller owns the transaction."""

import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.ledger.models import Account, Asset, Holding, Transaction
from finance_app.ledger.service import account_cash_balance
from finance_app.portfolio.models import PortfolioSnapshot, PriceSnapshot
from finance_app.portfolio.rules import SHANGHAI, aware, expected_nav_date, nav_is_fresh


def value_cents(quantity: Decimal, price: Decimal) -> int:
    for value in (quantity, price):
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise ValueError("quantity and price must be finite nonnegative decimals")
    qn, qd = quantity.as_integer_ratio()
    pn, pd = price.as_integer_ratio()
    numerator, denominator = qn * pn * 100, qd * pd
    result = (2 * numerator + denominator) // (2 * denominator)
    if result > 2**63 - 1:
        raise ValueError("valuation exceeds integer range")
    return result


def price_source_priority(source: str) -> int:
    if source == "manual" or source.startswith("manual:"):
        return 0
    if source == "eastmoney":
        return 1
    if source == "efinance":
        return 2
    return 3


def price_selection_key(row: PriceSnapshot) -> tuple[date, int, datetime, int]:
    """Sort newer NAVs first while honoring same-day source authority."""
    return (
        row.valuation_date,
        -price_source_priority(row.source),
        row.fetched_at,
        row.id,
    )


def refresh_current_snapshot_after_price_update(
    session: Session,
    *,
    now: datetime,
    holidays: frozenset[date] = frozenset(),
) -> PortfolioSnapshot | None:
    """Revalue today's existing snapshot after a confirmed price edit.

    A price edit must not silently confirm cash or holdings. Confirmation
    timestamps are carried forward only when the existing snapshot recorded
    them; missing or malformed timestamps remain unconfirmed.
    """
    aware(now)
    business_date = now.astimezone(SHANGHAI).date()
    row = session.scalar(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.snapshot_date == business_date
        )
    )
    if row is None:
        return None

    details: dict[str, object] = {}
    if row.details_json:
        try:
            parsed = json.loads(row.details_json)
            if isinstance(parsed, dict):
                details = parsed
        except (TypeError, ValueError):
            details = {}

    def timestamp(key: str) -> datetime | None:
        value = details.get(key)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    return create_daily_snapshot(
        session,
        now=now,
        holdings_confirmed_at=timestamp("holdings_confirmed_at"),
        cash_confirmed_at=timestamp("cash_confirmed_at"),
        holidays=holidays,
    )


def create_daily_snapshot(
    session: Session,
    *,
    now: datetime,
    holdings_confirmed_at: datetime | None = None,
    cash_confirmed_at: datetime | None = None,
    holidays: frozenset[date] = frozenset(),
) -> PortfolioSnapshot:
    aware(now)
    expected = expected_nav_date(now, holidays)
    business_date = now.astimezone(SHANGHAI).date()

    def confirmed(timestamp: datetime | None) -> bool:
        if timestamp is None:
            return False
        aware(timestamp)
        if timestamp > now:
            raise ValueError("confirmation cannot be in the future")
        return timestamp.astimezone(SHANGHAI).date() >= expected

    holdings_fresh = confirmed(holdings_confirmed_at)
    cash_fresh = confirmed(cash_confirmed_at)
    accounts = list(session.scalars(select(Account)))
    # This API values current confirmed balances, not historical ledger replay.
    future_transaction = session.scalar(
        select(Transaction.id).where(Transaction.occurred_at > now).limit(1)
    )
    if future_transaction is not None:
        cash_fresh = False
    currency_supported = all(account.currency == "CNY" for account in accounts)
    cash = sum(
        account_cash_balance(session, account.id)
        for account in accounts
        if account.currency == "CNY"
    )
    complete = holdings_fresh and cash_fresh and currency_supported
    invested = 0
    details: list[dict[str, object]] = []
    for holding in session.scalars(
        select(Holding).where(Holding.quantity > 0).order_by(Holding.id)
    ):
        asset = session.get(Asset, holding.asset_id)
        account = session.get(Account, holding.account_id)
        eligible = list(
            session.scalars(
                select(PriceSnapshot)
                .where(
                    PriceSnapshot.asset_id == holding.asset_id,
                    PriceSnapshot.valuation_date <= business_date,
                    PriceSnapshot.fetched_at <= now,
                    PriceSnapshot.error_text.is_(None),
                    PriceSnapshot.quote_type == "official_nav",
                )
                .order_by(
                    PriceSnapshot.valuation_date.desc(),
                )
            )
        )
        eligible.sort(
            key=price_selection_key,
            reverse=True,
        )
        price = next(
            (
                row
                for row in eligible
                if row.price.is_finite()
                and row.price > 0
                and row.source.strip()
                and row.valuation_date <= row.fetched_at.astimezone(SHANGHAI).date()
            ),
            None,
        )
        supported = (
            asset is not None
            and asset.currency == "CNY"
            and account is not None
            and account.currency == "CNY"
        )
        amount = (
            value_cents(holding.quantity, price.price) if price and supported else None
        )
        fresh = (
            price is not None
            and supported
            and nav_is_fresh(
                price.valuation_date, price.fetched_at, now, holidays=holidays
            )
        )
        complete = complete and fresh
        if amount is not None:
            invested += amount
        details.append(
            {
                "holding_id": holding.id,
                "asset_id": holding.asset_id,
                "quantity": str(holding.quantity),
                "value_cents": amount,
                "source": price.source if price else None,
                "valuation_date": price.valuation_date.isoformat() if price else None,
                "fetched_at": price.fetched_at.isoformat() if price else None,
                "price_fresh": fresh,
                "holding_updated_at": holding.updated_at.isoformat(),
            }
        )
    known = invested + cash
    if not 0 <= known <= 2**63 - 1:
        raise ValueError("portfolio valuation exceeds integer range")
    row = session.scalar(
        select(PortfolioSnapshot).where(
            PortfolioSnapshot.snapshot_date == business_date
        )
    )
    if row is None:
        row = PortfolioSnapshot(snapshot_date=business_date)
        session.add(row)
    elif row.details_json:
        previous = json.loads(row.details_json).get("as_of")
        if previous and datetime.fromisoformat(previous) > now:
            raise ValueError("snapshot time cannot move backwards")
    row.invested_value_cents = invested
    row.cash_value_cents = cash
    row.known_value_cents = known
    row.total_value_cents = known if complete else None
    row.data_complete = complete
    row.details_json = json.dumps(
        {
            "as_of": now.isoformat(),
            "holdings": details,
            "holdings_fresh": holdings_fresh,
            "cash_fresh": cash_fresh,
            "holdings_confirmed_at": holdings_confirmed_at.isoformat()
            if holdings_confirmed_at
            else None,
            "cash_confirmed_at": cash_confirmed_at.isoformat()
            if cash_confirmed_at
            else None,
            "calendar": "weekday-plus-supplied-holidays",
            "currency_supported": currency_supported,
        },
        ensure_ascii=False,
    )
    session.flush()
    return row
