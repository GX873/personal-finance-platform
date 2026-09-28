from datetime import UTC, date, datetime

from finance_app.portfolio.models import OpportunityAlert
from finance_app.portfolio.opportunities import (
    OpportunityAction,
    OpportunityCandidate,
    evaluate_opportunity,
    opportunity_already_emitted,
)
from tests.test_database import db_session as database_session_fixture

db_session = database_session_fixture


def candidate(**overrides):
    values = {
        "code": "000001",
        "name": "Candidate",
        "allocation_gap_bps": 1200,
        "overlap_bps": 2000,
        "valuation_percentile": 45,
        "recent_gain_bps": 100,
        "drawdown_bps": 700,
        "history_days": 500,
        "data_fresh": True,
        "available_cents": 10000,
    }
    values.update(overrides)
    return OpportunityCandidate(**values)


def test_candidate_is_rejected_when_recent_rally_is_overheated():
    result = evaluate_opportunity(candidate(recent_gain_bps=1800, drawdown_bps=0))
    assert result.action is OpportunityAction.WATCH
    assert result.reason_code == "RECENT_RALLY"


def test_candidate_is_eligible_when_allocation_gap_and_valuation_are_favorable():
    result = evaluate_opportunity(candidate())
    assert result.action is OpportunityAction.BUY_IN_BATCHES
    assert result.amount_cents == 10000


def test_same_opportunity_is_not_emitted_twice_in_one_salary_cycle(db_session):
    cycle_start = date(2026, 9, 15)
    assert not opportunity_already_emitted(
        db_session, code="000001", cycle_start=cycle_start
    )
    db_session.add(
        OpportunityAlert(
            code="000001",
            cycle_start=cycle_start,
            action="BUY_IN_BATCHES",
            amount_cents=10000,
            reason_code="FAVORABLE_ALLOCATION_AND_VALUATION",
            data_as_of=datetime(2026, 9, 28, tzinfo=UTC),
            used_special_budget=True,
        )
    )
    db_session.flush()
    assert opportunity_already_emitted(
        db_session, code="000001", cycle_start=cycle_start
    )
