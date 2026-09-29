"""Read-only dashboard data; missing observations never become zero balances."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
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
from finance_app.portfolio.rules import SHANGHAI
from finance_app.portfolio.service import value_cents


def money(cents: int | None) -> str:
    return "待补充" if cents is None else f"{Decimal(cents) / 100:,.2f}"


def dashboard(db: Session | None = None, *, section: str = "dashboard") -> dict:
    now = utc_now()
    today = now.astimezone(SHANGHAI).date()
    if section == "alerts":
        return {
            "today": today.isoformat(),
            "alerts": list(
                db.scalars(
                    select(Alert)
                    .order_by(Alert.created_at.desc(), Alert.id.desc())
                    .limit(100)
                )
            )
            if db
            else [],
        }
    snapshots = (
        list(
            db.scalars(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.snapshot_date <= today,
                    PortfolioSnapshot.snapshot_date >= today - timedelta(days=6),
                )
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(7)
            )
        )
        if db and section == "dashboard"
        else []
    )
    latest = (
        (
            snapshots[0]
            if snapshots
            else db.scalar(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.snapshot_date <= today)
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            )
        )
        if db and section == "dashboard"
        else None
    )
    buckets = (
        list(
            db.scalars(
                select(CashBucket).where(
                    CashBucket.bucket_kind.in_(("reserve", "investment"))
                )
            )
        )
        if db and section == "dashboard"
        else []
    )
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
    priced_holdings_total = 0
    if db:
        for holding, asset, account in db.execute(
            select(Holding, Asset, Account)
            .join(Asset, Holding.asset_id == Asset.id)
            .join(Account, Holding.account_id == Account.id)
            .where(Holding.quantity > 0)
            .order_by(Holding.id)
        ):
            price_query = (
                select(PriceSnapshot)
                    .where(
                        PriceSnapshot.asset_id == asset.id,
                        PriceSnapshot.valuation_date <= today,
                        PriceSnapshot.fetched_at <= now,
                        PriceSnapshot.error_text.is_(None),
                        PriceSnapshot.price > 0,
                        func.length(func.trim(PriceSnapshot.source)) > 0,
                        PriceSnapshot.valuation_date
                        <= func.date(PriceSnapshot.fetched_at, "+8 hours"),
                    )
                    .order_by(
                        PriceSnapshot.valuation_date.desc(),
                        case(
                            (PriceSnapshot.source == "manual", 0),
                            (PriceSnapshot.source.like("manual:%"), 0),
                            (PriceSnapshot.source == "eastmoney", 1),
                            (PriceSnapshot.source == "efinance", 2),
                            else_=3,
                        ),
                        PriceSnapshot.fetched_at.desc(),
                        PriceSnapshot.id.desc(),
                    )
                    .limit(1)
            )
            official = db.scalar(
                price_query.where(PriceSnapshot.quote_type == "official_nav")
            )
            price = official or db.scalar(
                price_query.where(PriceSnapshot.quote_type == "intraday_estimate")
            )
            estimate = price is not None and price.quote_type == "intraday_estimate"
            amount = (
                value_cents(holding.quantity, price.price)
                if (
                    price
                    and not estimate
                    and holding.quantity > 0
                    and asset.currency == account.currency == "CNY"
                )
                else None
            )
            if amount and asset.portfolio_role in role_values:
                role_values[asset.portfolio_role] += amount
            if amount:
                priced_holdings_total += amount
            holdings.append(
                {
                    "name": asset.name,
                    "code": asset.code,
                    "account": account.name,
                    "quantity": format(holding.quantity.normalize(), "f"),
                    "cost": money(holding.cost_cents),
                    "currency": asset.currency,
                    "role": "核心" if asset.portfolio_role == "core" else "卫星",
                    "nav": format(price.price.normalize(), "f") if price else "待补充",
                    "date": price.valuation_date.isoformat() if price else "—",
                    "source": price.source if price else "无有效来源",
                    "estimate": estimate,
                    "quote_type": price.quote_type if price else None,
                    "value": money(amount),
                    "fresh": bool(price and not estimate),
                    "priced": amount is not None,
                    "updated_at": holding.updated_at,
                }
            )
    if section == "holdings":
        return {"today": today.isoformat(), "holdings": holdings}
    cash_total_cents = (
        balances["reserve"] + balances["investment"]
        if balances["reserve"] is not None and balances["investment"] is not None
        else None
    )
    holdings_complete = len(holdings) == 0 or all(
        holding["priced"] for holding in holdings
    )
    current_total_cents = (
        priced_holdings_total + cash_total_cents
        if holdings_complete and cash_total_cents is not None
        else None
    )
    series = {
        row.snapshot_date: row.total_value_cents
        for row in snapshots
        if row.data_complete and row.total_value_cents is not None
    }
    if current_total_cents is not None:
        series[today] = current_total_cents
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
    display_total_cents = current_total_cents
    if display_total_cents is None and latest and latest.data_complete:
        display_total_cents = latest.total_value_cents
    known_cash_cents = sum(value for value in balances.values() if value is not None)
    known_cents = (
        priced_holdings_total + known_cash_cents
        if priced_holdings_total or known_cash_cents
        else (latest.known_value_cents if latest else None)
    )
    cash_segments = [
        {
            "label": label,
            "kind": kind,
            "value_cents": balances[kind] or 0,
            "amount": money(balances[kind]),
            "percent": (
                (balances[kind] or 0) / cash_total_cents * 100
                if cash_total_cents
                else 0
            ),
        }
        for kind, label in (
            ("reserve", "生活备用金"),
            ("investment", "可投资现金"),
        )
    ]
    role_total_cents = sum(role_values.values())
    role_segments = [
        {
            "label": label,
            "kind": kind,
            "value_cents": role_values[kind],
            "amount": money(role_values[kind]),
            "percent": (
                role_values[kind] / role_total_cents * 100
                if role_total_cents
                else 0
            ),
        }
        for kind, label in (("core", "核心资产"), ("satellite", "卫星资产"))
    ]
    return {
        "today": today.isoformat(),
        "total": money(display_total_cents) if display_total_cents is not None else "待补充",
        "snapshot_date": latest.snapshot_date.isoformat() if latest else "尚无快照",
        "known": money(known_cents) if display_total_cents is None and known_cents else None,
        "freshness_label": "数据已录入" if display_total_cents is not None else "待补充",
        "reserve": money(balances["reserve"]),
        "investment": money(balances["investment"]),
        "reserve_date": balance_dates["reserve"],
        "investment_date": balance_dates["investment"],
        "holdings": holdings,
        "priced_count": sum(h["priced"] for h in holdings),
        "role_values": role_values,
        "priced_total": sum(role_values.values()),
        "cash_total": money(cash_total_cents),
        "cash_total_cents": cash_total_cents or 0,
        "cash_chart": {
            "total_cents": cash_total_cents or 0,
            "segments": cash_segments,
        },
        "role_chart": {
            "total_cents": role_total_cents,
            "segments": role_segments,
        },
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
        "advice_action": "HOLD" if display_total_cents is not None else "WAIT_FOR_DATA",
        "advice_heading": "数据已录入，可以查看资产分布。"
        if display_total_cents is not None
        else "先把缺失数据补齐。",
        "advice_status": "已录入" if display_total_cents is not None else "待补充",
        "advice_reason": "总资产按已记录的现金、持仓份额和正式净值计算；系统不会因为日期较早要求重复确认。"
        if display_total_cents is not None
        else "缺少现金、持仓或正式净值，暂不把缺失金额当作零。",
    }
