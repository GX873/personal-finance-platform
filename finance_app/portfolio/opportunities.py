"""Deterministic screening for event-driven fund opportunities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.portfolio.models import OpportunityAlert


class OpportunityAction(StrEnum):
    WATCH = "WATCH"
    BUY_IN_BATCHES = "BUY_IN_BATCHES"


@dataclass(frozen=True)
class OpportunityCandidate:
    code: str
    name: str
    allocation_gap_bps: int
    overlap_bps: int
    valuation_percentile: int
    recent_gain_bps: int
    drawdown_bps: int
    history_days: int
    data_fresh: bool
    available_cents: int


@dataclass(frozen=True)
class OpportunityDecision:
    action: OpportunityAction
    reason_code: str
    amount_cents: int


def evaluate_opportunity(candidate: OpportunityCandidate) -> OpportunityDecision:
    for value in (
        candidate.allocation_gap_bps,
        candidate.overlap_bps,
        candidate.valuation_percentile,
        candidate.recent_gain_bps,
        candidate.drawdown_bps,
        candidate.history_days,
        candidate.available_cents,
    ):
        if type(value) is not int:
            raise ValueError("opportunity metrics must be integers")
    if not candidate.data_fresh or candidate.history_days < 250:
        return OpportunityDecision(OpportunityAction.WATCH, "DATA_INSUFFICIENT", 0)
    if candidate.allocation_gap_bps < 500:
        return OpportunityDecision(OpportunityAction.WATCH, "NO_ALLOCATION_GAP", 0)
    if candidate.overlap_bps > 6000:
        return OpportunityDecision(OpportunityAction.WATCH, "HOLDING_OVERLAP", 0)
    if candidate.valuation_percentile > 70:
        return OpportunityDecision(OpportunityAction.WATCH, "VALUATION_HIGH", 0)
    if candidate.recent_gain_bps > 1200:
        return OpportunityDecision(OpportunityAction.WATCH, "RECENT_RALLY", 0)
    if candidate.drawdown_bps > 2000:
        return OpportunityDecision(OpportunityAction.WATCH, "FALLING_TOO_FAST", 0)
    amount = min(candidate.available_cents, 10000)
    if amount <= 0:
        return OpportunityDecision(OpportunityAction.WATCH, "NO_INVESTMENT_CASH", 0)
    return OpportunityDecision(
        OpportunityAction.BUY_IN_BATCHES, "FAVORABLE_ALLOCATION_AND_VALUATION", amount
    )


def opportunity_already_emitted(
    session: Session, *, code: str, cycle_start
) -> bool:
    return (
        session.scalar(
            select(OpportunityAlert.id).where(
                OpportunityAlert.code == code,
                OpportunityAlert.cycle_start == cycle_start,
            )
        )
        is not None
    )
