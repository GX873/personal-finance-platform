"""Database-only view models for the fund data center."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_app.ledger.models import Asset, Holding
from finance_app.market.analytics import (
    FundMetrics,
    Period,
    calculate_metrics,
    period_start,
)
from finance_app.market.base import QuoteType
from finance_app.portfolio.models import PriceSnapshot
from finance_app.portfolio.rules import SHANGHAI, aware
from finance_app.portfolio.service import price_selection_key, value_cents

_PERIOD_VALUES = frozenset(item.value for item in Period)
_EMPTY_METRICS = FundMetrics(None, None, None, None)
_RATIO_SCALE = Decimal("0.00000001")
_CHART_VIEW_BOX = "0 0 600 220"


@dataclass(frozen=True)
class HeldFund:
    asset_id: int
    code: str
    name: str
    quantity: Decimal
    cost_cents: int


def build_fund_center(
    db: Session,
    *,
    selected_asset_id: int | None,
    period: str,
    now: datetime,
) -> dict[str, object]:
    """Build fund pages entirely from validated rows already stored locally."""
    aware(now)
    selected_period = Period(period) if period in _PERIOD_VALUES else Period.ONE_YEAR
    holdings = _aggregate_held_funds(db)
    if not holdings:
        return _empty_fund_center(selected_period)

    rows = [
        _serialize_fund(db, holding, selected_period, now) for holding in holdings
    ]
    known_total_cents = sum(
        row["official_value_cents"]
        for row in rows
        if isinstance(row["official_value_cents"], int)
    )
    values_complete = all(
        row["official_value_cents"] is not None for row in rows
    )
    for row in rows:
        official_value = row["official_value_cents"]
        row["portfolio_percent"] = (
            _ratio(official_value, known_total_cents)
            if values_complete
            and isinstance(official_value, int)
            and known_total_cents > 0
            else None
        )
    rows.sort(
        key=lambda row: (
            row["official_value_cents"] is None,
            -row["official_value_cents"]
            if isinstance(row["official_value_cents"], int)
            else 0,
            row["code"],
        )
    )
    selected = next(
        (row for row in rows if row["asset_id"] == selected_asset_id), rows[0]
    )
    history = _official_history(
        db, int(selected["asset_id"]), selected_period, now
    )
    metrics = calculate_metrics([row.price for row in history])
    serialized_history = [_serialize_history_point(row) for row in history]
    return {
        "period": selected_period.value,
        "periods": [item.value for item in Period],
        "funds": rows,
        "selected": selected,
        "history": serialized_history,
        "metrics": _serialize_metrics(metrics),
        "chart": _chart(serialized_history),
    }


def _aggregate_held_funds(db: Session) -> list[HeldFund]:
    rows = db.execute(
        select(
            Asset.id,
            Asset.code,
            Asset.name,
            func.sum(Holding.quantity),
            func.sum(Holding.cost_cents),
        )
        .join(Holding, Holding.asset_id == Asset.id)
        .where(
            Holding.quantity > 0,
            Asset.asset_class == "fund",
            Asset.market == "CN",
        )
        .group_by(Asset.id, Asset.code, Asset.name)
        .order_by(Asset.code, Asset.id)
    )
    return [
        HeldFund(
            asset_id=asset_id,
            code=code,
            name=name,
            quantity=Decimal(quantity),
            cost_cents=int(cost_cents),
        )
        for asset_id, code, name, quantity, cost_cents in rows
        if Decimal(quantity) > 0
    ]


def _serialize_fund(
    db: Session,
    holding: HeldFund,
    period: Period,
    now: datetime,
) -> dict[str, object]:
    official = _latest_quote(db, holding.asset_id, QuoteType.OFFICIAL_NAV, now)
    official_value = (
        value_cents(holding.quantity, official.price) if official is not None else None
    )
    history = _official_history(db, holding.asset_id, period, now)
    metrics = calculate_metrics([row.price for row in history])
    return {
        "asset_id": holding.asset_id,
        "code": holding.code,
        "name": holding.name,
        "quantity": holding.quantity,
        "cost_cents": holding.cost_cents,
        "official_nav": _price_text(official),
        "official_value_cents": official_value,
        "official_profit_cents": _profit(official_value, holding.cost_cents),
        "holding_return": _return(official_value, holding.cost_cents),
        "official_date": _date_text(official),
        "official_fetched_at": _datetime_text(official),
        "data_source": official.source if official is not None else None,
        "source_url": official.source_url if official is not None else None,
        "portfolio_percent": None,
        **_serialize_metrics(metrics),
    }


def _latest_quote(
    db: Session,
    asset_id: int,
    quote_type: QuoteType,
    now: datetime,
) -> PriceSnapshot | None:
    rows = db.scalars(
        _usable_quote_query(asset_id, quote_type, now).order_by(
            PriceSnapshot.valuation_date.desc(),
            PriceSnapshot.fetched_at.desc(),
            PriceSnapshot.id.desc(),
        )
    )
    return max(
        (row for row in rows if _valid_quote(row, now)),
        key=price_selection_key,
        default=None,
    )


def _official_history(
    db: Session,
    asset_id: int,
    period: Period,
    now: datetime,
) -> list[PriceSnapshot]:
    query = _usable_quote_query(asset_id, QuoteType.OFFICIAL_NAV, now)
    cutoff = period_start(period, now.astimezone(SHANGHAI).date())
    if cutoff is not None:
        query = query.where(PriceSnapshot.valuation_date >= cutoff)
    rows = db.scalars(
        query.order_by(
            PriceSnapshot.valuation_date,
            PriceSnapshot.fetched_at,
            PriceSnapshot.id,
        )
    )
    by_date: dict[object, PriceSnapshot] = {}
    for row in rows:
        if not _valid_quote(row, now):
            continue
        current = by_date.get(row.valuation_date)
        if current is None or price_selection_key(row) > price_selection_key(current):
            by_date[row.valuation_date] = row
    return [by_date[day] for day in sorted(by_date)]


def _usable_quote_query(
    asset_id: int,
    quote_type: QuoteType,
    now: datetime,
):
    return select(PriceSnapshot).where(
        PriceSnapshot.asset_id == asset_id,
        PriceSnapshot.quote_type == quote_type.value,
        PriceSnapshot.price > 0,
        PriceSnapshot.error_text.is_(None),
        PriceSnapshot.fetched_at <= now,
        PriceSnapshot.valuation_date <= now.astimezone(SHANGHAI).date(),
        func.length(func.trim(PriceSnapshot.source)) > 0,
    )


def _valid_quote(row: PriceSnapshot, now: datetime) -> bool:
    return (
        row.price.is_finite()
        and row.price > 0
        and bool(row.source.strip())
        and row.fetched_at <= now
        and row.valuation_date <= now.astimezone(SHANGHAI).date()
        and row.valuation_date <= row.fetched_at.astimezone(SHANGHAI).date()
        and row.error_text is None
    )


def _serialize_history_point(row: PriceSnapshot) -> dict[str, object]:
    return {
        "valuation_date": row.valuation_date.isoformat(),
        "price": format(row.price, ".8f"),
        "quote_type": row.quote_type,
        "source": row.source,
        "fetched_at": row.fetched_at.isoformat(),
    }


def _serialize_metrics(metrics: FundMetrics) -> dict[str, object]:
    return {
        "total_return": metrics.total_return,
        "max_drawdown": metrics.max_drawdown,
        "annualized_volatility": metrics.annualized_volatility,
        "risk_label": metrics.risk_label,
    }


def _chart(history: list[dict[str, object]]) -> dict[str, object]:
    if not history:
        return {"view_box": _CHART_VIEW_BOX, "points": [], "polyline": ""}
    prices = [Decimal(str(point["price"])) for point in history]
    low, high = min(prices), max(prices)
    count = len(history)
    points = []
    for index, (history_point, price) in enumerate(zip(history, prices, strict=True)):
        x = (
            Decimal(300)
            if count == 1
            else Decimal(20) + Decimal(index * 560) / Decimal(count - 1)
        )
        y = (
            Decimal(105)
            if high == low
            else Decimal(190) - (price - low) / (high - low) * Decimal(170)
        )
        points.append(
            {
                "x": _coordinate(x),
                "y": _coordinate(y),
                "valuation_date": history_point["valuation_date"],
                "price": history_point["price"],
            }
        )
    return {
        "view_box": _CHART_VIEW_BOX,
        "points": points,
        "polyline": " ".join(f'{point["x"]},{point["y"]}' for point in points),
    }


def _empty_fund_center(period: Period) -> dict[str, object]:
    return {
        "period": period.value,
        "periods": [item.value for item in Period],
        "funds": [],
        "selected": {
            "asset_id": None,
            "code": None,
            "name": None,
            "quantity": None,
            "cost_cents": None,
            "official_nav": None,
            "official_value_cents": None,
            "official_profit_cents": None,
            "holding_return": None,
            "official_date": None,
            "official_fetched_at": None,
            "data_source": None,
            "source_url": None,
            "portfolio_percent": None,
            **_serialize_metrics(_EMPTY_METRICS),
        },
        "history": [],
        "metrics": _serialize_metrics(_EMPTY_METRICS),
        "chart": _chart([]),
    }


def _price_text(row: PriceSnapshot | None) -> str | None:
    return format(row.price, ".8f") if row is not None else None


def _date_text(row: PriceSnapshot | None) -> str | None:
    return row.valuation_date.isoformat() if row is not None else None


def _datetime_text(row: PriceSnapshot | None) -> str | None:
    return row.fetched_at.isoformat() if row is not None else None


def _profit(value: int | None, cost: int) -> int | None:
    return value - cost if value is not None else None


def _return(value: int | None, cost: int) -> Decimal | None:
    return _ratio(value - cost, cost) if value is not None and cost > 0 else None


def _ratio(numerator: int, denominator: int) -> Decimal:
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        _RATIO_SCALE, rounding=ROUND_HALF_UP
    )


def _coordinate(value: Decimal) -> int | float:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(rounded) if rounded == rounded.to_integral_value() else float(rounded)
