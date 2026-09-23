"""Read-only dashboard data; missing observations never become zero balances."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.db import utc_now
from finance_app.ledger.budget import BucketKind, default_budget_rule
from finance_app.ledger.models import Account, Asset, CashBucket, Holding
from finance_app.portfolio.models import (
    Alert,
    AllocationTarget,
    PortfolioSnapshot,
    PriceSnapshot,
)
from finance_app.portfolio.rules import SHANGHAI, nav_is_fresh
from finance_app.portfolio.service import value_cents


def money(cents: int | None) -> str:
    return "待确认" if cents is None else f"{Decimal(cents) / 100:,.2f}"


def dashboard(db: Session | None = None) -> dict:
    now = utc_now()
    today = now.astimezone(SHANGHAI).date()
    snapshots = (
        list(
            db.scalars(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.snapshot_date <= today)
                .order_by(PortfolioSnapshot.snapshot_date.desc())
            )
        )
        if db
        else []
    )
    latest = snapshots[0] if snapshots else None
    buckets = list(db.scalars(select(CashBucket))) if db else []
    balances = {}
    balance_dates = {}
    for kind in ("reserve", "investment"):
        rows = [row for row in buckets if row.bucket_kind == kind]
        balances[kind] = sum(row.balance_cents for row in rows) if rows else None
        balance_dates[kind] = (
            min(row.updated_at for row in rows)
            .astimezone(SHANGHAI)
            .strftime("%Y-%m-%d")
            if rows
            else "尚未记录"
        )
    holdings = []
    role_values = {"core": 0, "satellite": 0}
    if db:
        for holding, asset, account in db.execute(
            select(Holding, Asset, Account)
            .join(Asset, Holding.asset_id == Asset.id)
            .join(Account, Holding.account_id == Account.id)
            .where(Holding.quantity > 0)
            .order_by(Holding.id)
        ):
            prices = db.scalars(
                select(PriceSnapshot)
                .where(
                    PriceSnapshot.asset_id == asset.id,
                    PriceSnapshot.valuation_date <= today,
                    PriceSnapshot.fetched_at <= now,
                    PriceSnapshot.error_text.is_(None),
                )
                .order_by(
                    PriceSnapshot.valuation_date.desc(),
                    PriceSnapshot.fetched_at.desc(),
                    PriceSnapshot.id.desc(),
                )
            )
            price = next(
                (
                    p
                    for p in prices
                    if p.price.is_finite()
                    and p.price > 0
                    and p.source.strip()
                    and p.valuation_date <= p.fetched_at.astimezone(SHANGHAI).date()
                ),
                None,
            )
            amount = (
                value_cents(holding.quantity, price.price)
                if (
                    price
                    and holding.quantity > 0
                    and asset.currency == account.currency == "CNY"
                )
                else None
            )
            if amount and asset.portfolio_role in role_values:
                role_values[asset.portfolio_role] += amount
            holdings.append(
                {
                    "name": asset.name,
                    "code": asset.code,
                    "account": account.name,
                    "quantity": format(holding.quantity.normalize(), "f"),
                    "cost": money(holding.cost_cents),
                    "currency": asset.currency,
                    "role": "核心" if asset.portfolio_role == "core" else "卫星",
                    "nav": format(price.price.normalize(), "f") if price else "待确认",
                    "date": price.valuation_date.isoformat() if price else "—",
                    "source": price.source if price else "无有效来源",
                    "value": money(amount),
                    "fresh": bool(
                        price
                        and nav_is_fresh(price.valuation_date, price.fetched_at, now)
                    ),
                    "priced": amount is not None,
                }
            )
    series = {
        row.snapshot_date: row.total_value_cents
        for row in snapshots
        if row.data_complete and row.total_value_cents is not None
    }
    days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    values = [series[day] for day in days if day in series]
    low, high = (min(values), max(values)) if values else (0, 0)
    points, lines = [], []
    previous = None
    for index, day in enumerate(days):
        if day not in series:
            previous = None
            continue
        point = {
            "x": 25 + index * 85,
            "y": 95
            if high == low
            else round(155 - (series[day] - low) / (high - low) * 120, 2),
            "label": f"{day.isoformat()} ¥{money(series[day])}",
        }
        points.append(point)
        if previous:
            lines.append((previous, point))
        previous = point
    rule = default_budget_rule()
    labels = ["固定支出", "备用金", "投资", "自由支配"]
    targets = (
        list(db.scalars(select(AllocationTarget).order_by(AllocationTarget.id)))
        if db
        else []
    )
    return {
        "today": today.isoformat(),
        "total": money(latest.total_value_cents)
        if latest and latest.data_complete and latest.total_value_cents is not None
        else "未确认",
        "snapshot_date": latest.snapshot_date.isoformat() if latest else "尚无快照",
        "known": money(latest.known_value_cents) if latest else None,
        "reserve": money(balances["reserve"]),
        "investment": money(balances["investment"]),
        "reserve_date": balance_dates["reserve"],
        "investment_date": balance_dates["investment"],
        "holdings": holdings,
        "priced_count": sum(h["priced"] for h in holdings),
        "role_values": role_values,
        "priced_total": sum(role_values.values()),
        "budget": [
            {
                "label": label,
                "amount": money(getattr(rule, f"{kind.value}_cents")),
                "percent": getattr(rule, f"{kind.value}_cents") / 4000,
            }
            for kind, label in zip(BucketKind, labels, strict=True)
        ],
        "targets": targets,
        "chart_points": points,
        "chart_lines": lines,
        "chart_days": [d.strftime("%m/%d") for d in days],
        "chart_low": money(low) if values else None,
        "chart_high": money(high) if values else None,
        "alerts": list(
            db.scalars(
                select(Alert)
                .order_by(Alert.created_at.desc(), Alert.id.desc())
                .limit(100)
            )
        )
        if db
        else [],
        "advice_action": "WAIT_FOR_DATA",
        "advice_reason": "尚未完成适用配置与数据的联合确认。请先确认现金、持仓、净值与投资配置，再评估操作。",
    }
