from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from finance_app.market.base import QuoteType
from finance_app.market.efinance_adapter import EfinanceAdapter
from finance_app.market.xalpha_adapter import XalphaAdapter

NOW = datetime(2026, 9, 28, 6, tzinfo=UTC)


class Row(dict):
    pass


class Frame:
    def __init__(self, row):
        self.index = [0]
        self.iloc = [row]

    def iterrows(self):
        return iter(enumerate(self.iloc))


class FundClient:
    def get_quote_history(self, code, pz):
        assert code == "000001"
        assert pz in {1, 250}
        return Frame(Row({"单位净值": "1.2345", "净值日期": "2026-09-25"}))

    def get_invest_position(self, code):
        assert code == "000001"
        return PositionFrame(
            [
                Row({"股票代码": "600000", "持仓占比": "12.34"}),
                Row({"股票代码": "000001", "持仓占比": "5.00"}),
            ]
        )


class PositionFrame(Frame):
    def __init__(self, rows):
        self.rows = rows
        self.index = list(range(len(rows)))

    def iterrows(self):
        return iter(enumerate(self.rows))


def test_efinance_adapter_maps_official_nav_without_leaking_dataframe():
    quote = EfinanceAdapter(FundClient(), clock=lambda: NOW).fetch("000001")
    assert quote.value == Decimal("1.2345")
    assert quote.valuation_date == date(2026, 9, 25)
    assert quote.quote_type is QuoteType.OFFICIAL_NAV


def test_efinance_adapter_maps_disclosed_positions_to_basis_points():
    positions = EfinanceAdapter(FundClient(), clock=lambda: NOW).fetch_positions(
        "000001"
    )
    assert positions == {"600000": 1234, "000001": 500}


def test_efinance_adapter_maps_history_rows_for_screening():
    quotes = EfinanceAdapter(FundClient(), clock=lambda: NOW).fetch_history(
        "000001", limit=250
    )
    assert len(quotes) == 1
    assert quotes[0].valuation_date == date(2026, 9, 25)


def test_xalpha_boundary_calculates_return_drawdown_and_volatility():
    analytics = XalphaAdapter([Decimal(1), Decimal("1.2"), Decimal("0.9"), Decimal("1.1")])
    assert analytics.total_return() == Decimal("0.1")
    assert analytics.max_drawdown() == Decimal("-0.25")
    assert analytics.volatility() > 0


def test_xalpha_boundary_annualizes_sample_daily_volatility():
    analytics = XalphaAdapter([Decimal(1), Decimal("1.01"), Decimal("1.02")])
    daily = analytics.volatility() / Decimal(252).sqrt()
    assert daily > 0
