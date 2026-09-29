"""Read-only dashboard data; missing observations never become zero balances."""

import json
from datetime import datetime, timedelta
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
from finance_app.portfolio.rules import SHANGHAI, expected_nav_date, nav_is_fresh
from finance_app.portfolio.service import value_cents


def money(cents: int | None) -> str:
    return "待确认" if cents is None else f"{Decimal(cents) / 100:,.2f}"


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
    if db:
        for holding, asset, account in db.execute(
            select(Holding, Asset, Account)
            .join(Asset, Holding.asset_id == Asset.id)
            .join(Account, Holding.account_id == Account.id)
            .where(Holding.quantity > 0)
            .order_by(Holding.id)
        ):
            price = db.scalar(
                select(PriceSnapshot)
                .where(
                    PriceSnapshot.asset_id == asset.id,
                    PriceSnapshot.valuation_date <= today,
                    PriceSnapshot.fetched_at <= now,
                    PriceSnapshot.error_text.is_(None),
                    PriceSnapshot.quote_type == "official_nav",
                    PriceSnapshot.price > 0,
                    func.length(func.trim(PriceSnapshot.source)) > 0,
                    PriceSnapshot.valuation_date
                    <= func.date(PriceSnapshot.fetched_at, "+8 hours"),
                )
                .order_by(
                    PriceSnapshot.valuation_date.desc(),
                    case(
                        (PriceSnapshot.source.like("manual%"), 0),
                        (PriceSnapshot.source == "eastmoney", 1),
                        (PriceSnapshot.source == "efinance", 2),
                        else_=3,
                    ),
                    PriceSnapshot.fetched_at.desc(),
                    PriceSnapshot.id.desc(),
                )
                .limit(1)
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
                    "updated_at": holding.updated_at,
                }
            )
    if section == "holdings":
        return {"today": today.isoformat(), "holdings": holdings}
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
    complete = bool(
        latest and latest.data_complete and latest.total_value_cents is not None
    )
    # A complete historical valuation alone cannot certify today's data.
    fresh = False
    if complete and latest and latest.details_json:
        try:
            details = json.loads(latest.details_json)
            as_of = datetime.fromisoformat(details["as_of"])
            fresh = bool(
                as_of.utcoffset() is not None
                and as_of <= now
                and as_of.astimezone(SHANGHAI).date() >= expected_nav_date(now)
                and all(
                    details.get(key) is True
                    for key in ("holdings_fresh", "cash_fresh", "currency_supported")
                )
                and all(
                    balances[kind] is not None for kind in ("reserve", "investment")
                )
                and all(
                    expected_nav_date(now) <= row.updated_at.astimezone(SHANGHAI).date()
                    and row.updated_at <= as_of
                    for row in buckets
                )
                and all(
                    h["fresh"] and h["priced"] and h["updated_at"] <= as_of
                    for h in holdings
                )
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            fresh = False
    return {
        "today": today.isoformat(),
        "total": money(latest.total_value_cents)
        if latest and latest.data_complete and latest.total_value_cents is not None
        else "未确认",
        "snapshot_date": latest.snapshot_date.isoformat() if latest else "尚无快照",
        "known": money(latest.known_value_cents) if latest and not complete else None,
        "freshness_label": "数据有效"
        if fresh
        else "数据待更新"
        if complete
        else "数据待完善",
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
        "advice_action": "HOLD" if fresh else "WAIT_FOR_DATA",
        "advice_heading": "数据已就绪，先确认投资配置。"
        if fresh
        else "先把数据补齐，再做决定。",
        "advice_status": "待配置" if fresh else "等待确认",
        "advice_reason": "现金、持仓与净值数据有效。风险偏好、适用配置与本月剩余额度尚未联合确认，暂不建议新增操作。"
        if fresh
        else "请先补齐或更新现金、持仓与净值，并确认数据来源和日期，再评估操作。",
    }
