"""Pure conditional guidance; this module never places orders."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from finance_app.db import utc_now
from finance_app.ledger.models import RiskLevel

SHANGHAI = ZoneInfo("Asia/Shanghai")


class AdviceAction(StrEnum):
    BUY = "BUY"
    BUY_IN_BATCHES = "BUY_IN_BATCHES"
    HOLD = "HOLD"
    REDUCE_IN_BATCHES = "REDUCE_IN_BATCHES"
    SELL = "SELL"
    WAIT = "WAIT"
    WAIT_FOR_DATA = "WAIT_FOR_DATA"
    WATCH = "WATCH"


@dataclass(frozen=True)
class RuleContext:
    reserve_cents: int | None = None
    investment_cash_cents: int | None = None
    prices_fresh: bool = False
    holdings_fresh: bool = False
    cash_fresh: bool = False
    required_reserve_cents: int = 100000
    month_budget_remaining_cents: int | None = None
    special_opportunity_used: bool = False
    special_opportunity_cents: int = 0
    reserve_replenishment_cents: int = 0
    source: str = "confirmed-data"
    timestamp: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (self.prices_fresh, self.holdings_fresh, self.cash_fresh)
        ):
            raise ValueError("freshness flags must be booleans")
        for value in (
            self.reserve_cents,
            self.investment_cash_cents,
            self.required_reserve_cents,
            self.month_budget_remaining_cents,
            self.special_opportunity_cents,
            self.reserve_replenishment_cents,
        ):
            if value is not None and (
                type(value) is not int or not 0 <= value <= 2**63 - 1
            ):
                raise ValueError("money must be nonnegative integer cents")
        if self.required_reserve_cents is None:
            raise ValueError("reserve requirement is required")
        if type(self.special_opportunity_used) is not bool:
            raise ValueError("special opportunity usage must be boolean")
        aware(self.timestamp)
        if not self.source.strip():
            raise ValueError("source is required")


@dataclass(frozen=True)
class Advice:
    action: AdviceAction
    amount_cents: int | None
    reason_code: str
    trigger: str
    max_risk: str
    stop_discipline: str
    source: str
    timestamp: datetime
    conditional: bool = True
    asset_id: int | None = None
    code: str | None = None
    name: str | None = None
    current_bps: int | None = None
    upper_bps: int | None = None
    scope: str | None = None


def _advice(
    context: RuleContext,
    action: AdviceAction,
    amount: int | None,
    reason: str,
    trigger: str,
    *,
    asset_id: int | None = None,
    code: str | None = None,
    name: str | None = None,
    current_bps: int | None = None,
    upper_bps: int | None = None,
    scope: str | None = None,
) -> Advice:
    return Advice(
        action,
        amount,
        reason,
        trigger,
        "投资本金可能全部损失；此处不承诺最大亏损金额。",
        "执行前人工复核；止损阈值不是成交价或亏损上限保证。",
        context.source,
        context.timestamp,
        asset_id=asset_id,
        code=code,
        name=name,
        current_bps=current_bps,
        upper_bps=upper_bps,
        scope=scope,
    )


def evaluate_new_investment(
    context: RuleContext, risk_level: RiskLevel, *, now: datetime | None = None
) -> Advice:
    RiskLevel(risk_level)
    reference = now if now is not None else utc_now()
    aware(reference)
    if (
        context.timestamp > reference
        or not context.cash_fresh
        or context.reserve_cents is None
        or context.investment_cash_cents is None
        or context.month_budget_remaining_cents is None
        or not all((context.prices_fresh, context.holdings_fresh, context.cash_fresh))
    ):
        return _advice(
            context,
            AdviceAction.WAIT_FOR_DATA,
            None,
            "DATA_UNCONFIRMED",
            "请先补齐并确认现金、持仓和净值数据。",
        )
    if context.reserve_cents < context.required_reserve_cents:
        return _advice(
            context,
            AdviceAction.WAIT,
            0,
            "RESERVE_SHORTFALL",
            "备用金未达标，暂停新增投资。",
        )
    amount = min(context.investment_cash_cents, context.month_budget_remaining_cents)
    if amount == 0:
        return _advice(
            context,
            AdviceAction.WAIT,
            0,
            "NO_INVESTMENT_CASH",
            "可投资现金或本月剩余额度为零。",
        )
    return _advice(
        context,
        AdviceAction.BUY,
        amount,
        "CONDITIONAL_BUY",
        "仅在数据仍有效且人工确认风险后，可考虑不超过此金额的买入；不会自动下单。",
    )


def evaluate_reserve_shortfall(
    context: RuleContext, *, now: datetime | None = None
) -> Advice | None:
    reference = now if now is not None else utc_now()
    aware(reference)
    if (
        context.timestamp > reference
        or not context.cash_fresh
        or context.reserve_cents is None
        or context.reserve_cents >= context.required_reserve_cents
    ):
        return None
    return _advice(
        context,
        AdviceAction.WAIT,
        0,
        "RESERVE_SHORTFALL",
        "备用金未达标，暂停新增投资。",
    )


def evaluate_allocation(
    context: RuleContext,
    current_bps: int,
    position_value_cents: int,
    target_bps: int | None,
    upper_bps: int | None,
    *,
    invested_value_cents: int,
    batch_bps: int = 1000,
    now: datetime | None = None,
    asset_id: int | None = None,
    code: str | None = None,
    name: str | None = None,
    scope: str | None = None,
) -> Advice:
    for value in (current_bps, target_bps, upper_bps, batch_bps):
        if value is not None and (type(value) is not int or not 0 <= value <= 10000):
            raise ValueError("allocation must be integer basis points from 0 to 10000")
    if (
        type(position_value_cents) is not int
        or not 0 <= position_value_cents <= 2**63 - 1
    ):
        raise ValueError("invalid position value")
    if (
        type(invested_value_cents) is not int
        or not 0 < invested_value_cents <= 2**63 - 1
        or position_value_cents > invested_value_cents
    ):
        raise ValueError("invalid invested value")
    if target_bps is not None and upper_bps is not None and target_bps > upper_bps:
        raise ValueError("target cannot exceed upper allocation")
    reference = now if now is not None else utc_now()
    aware(reference)
    if context.timestamp > reference or not all(
        (context.prices_fresh, context.holdings_fresh, context.cash_fresh)
    ):
        return _advice(
            context,
            AdviceAction.WAIT_FOR_DATA,
            None,
            "DATA_UNCONFIRMED",
            "请先确认估值和持仓。",
            asset_id=asset_id,
            code=code,
            name=name,
            current_bps=current_bps,
            upper_bps=upper_bps,
            scope=scope,
        )
    if target_bps is None or upper_bps is None:
        return _advice(
            context,
            AdviceAction.HOLD,
            0,
            "ALLOCATION_UNCONFIGURED",
            "未配置目标和上限，暂不建议调整。",
            asset_id=asset_id,
            code=code,
            name=name,
            current_bps=current_bps,
            upper_bps=upper_bps,
            scope=scope,
        )
    if position_value_cents * 10000 > upper_bps * invested_value_cents:
        # Sale proceeds leave invested value, so both numerator and denominator shrink.
        numerator = (
            position_value_cents * 10000 - upper_bps * invested_value_cents
        )
        denominator = 10000 - upper_bps
        required = (numerator + denominator - 1) // denominator
        amount = min(
            position_value_cents,
            position_value_cents * batch_bps // 10000,
            required,
        )
        return _advice(
            context,
            AdviceAction.REDUCE_IN_BATCHES,
            amount,
            "ABOVE_UPPER",
            f"占比超过上限，可人工确认后分批减持；单批上限为持仓的{batch_bps}/10000。",
            asset_id=asset_id,
            code=code,
            name=name,
            current_bps=current_bps,
            upper_bps=upper_bps,
            scope=scope,
        )
    return _advice(
        context,
        AdviceAction.HOLD,
        0,
        "WITHIN_UPPER",
        "未超过配置上限，继续观察。",
        asset_id=asset_id,
        code=code,
        name=name,
        current_bps=current_bps,
        upper_bps=upper_bps,
        scope=scope,
    )


def aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")


def expected_nav_date(now: datetime, holidays: frozenset[date] = frozenset()) -> date:
    aware(now)
    if any(type(day) is not date for day in holidays):
        raise ValueError("holidays must contain validated dates")
    local = now.astimezone(SHANGHAI)
    day = local.date()
    if day.weekday() < 5 and local.hour < 22 and day not in holidays:
        day -= timedelta(days=1)
    while day.weekday() >= 5 or day in holidays:
        day -= timedelta(days=1)
    return day


def nav_is_fresh(
    valuation_date: date,
    fetched_at: datetime,
    now: datetime,
    *,
    holidays: frozenset[date] = frozenset(),
) -> bool:
    """Weekday calendar baseline; callers may supply verified market holidays."""
    aware(fetched_at)
    expected = expected_nav_date(now, holidays)
    if type(valuation_date) is not date:
        raise ValueError("valuation date must be a date")
    return (
        fetched_at <= now
        and valuation_date <= now.astimezone(SHANGHAI).date()
        and valuation_date <= fetched_at.astimezone(SHANGHAI).date()
        and valuation_date >= expected
    )
