"""Optional efinance bridge with a narrow, testable output contract."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from finance_app.db import utc_now
from finance_app.market.base import FundNavQuote, QuoteType


class EfinanceAdapter:
    source = "efinance"
    source_url = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHisNetList"

    def __init__(
        self,
        client: Any | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = client
        self._clock = clock

    def _fund_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import efinance as ef  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("optional dependency efinance is not installed") from exc
        self._client = ef.fund
        return self._client

    def fetch(self, fund_code: str) -> FundNavQuote:
        history = self._fund_client().get_quote_history(fund_code, pz=1)
        if history is None or len(history.index) == 0:
            raise RuntimeError("efinance returned no NAV rows")
        row = history.iloc[0]
        raw_date = row.get("日期") or row.get("净值日期")
        return FundNavQuote(
            value=Decimal(str(row["单位净值"])),
            valuation_date=date.fromisoformat(str(raw_date)),
            source=self.source,
            source_url=self.source_url,
            fetched_at=self._clock(),
            quote_type=QuoteType.OFFICIAL_NAV,
        )

    def fetch_history(self, fund_code: str, *, limit: int = 500) -> list[FundNavQuote]:
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("history limit must be from 1 to 5000")
        rows = self._fund_client().get_quote_history(fund_code, pz=limit)
        if rows is None or len(rows.index) == 0:
            return []
        fetched_at = self._clock()
        quotes = []
        for _, row in rows.iterrows():
            raw_date = row.get("日期") or row.get("净值日期")
            quotes.append(
                FundNavQuote(
                    value=Decimal(str(row["单位净值"])),
                    valuation_date=date.fromisoformat(str(raw_date)),
                    source=self.source,
                    source_url=self.source_url,
                    fetched_at=fetched_at,
                    quote_type=QuoteType.OFFICIAL_NAV,
                )
            )
        return quotes

    def fetch_positions(self, fund_code: str) -> dict[str, int]:
        """Return latest disclosed stock weights as integer basis points."""
        rows = self._fund_client().get_invest_position(fund_code)
        if rows is None or len(rows.index) == 0:
            return {}
        result: dict[str, int] = {}
        for _, row in rows.iterrows():
            code = str(row["股票代码"]).strip()
            weight_bps = int(Decimal(str(row["持仓占比"])) * 100)
            if code and 0 <= weight_bps <= 10000:
                result[code] = weight_bps
        return result


class EfinanceEstimateAdapter(EfinanceAdapter):
    source = "efinance:estimate"
    source_url = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo"

    def fetch(self, fund_code: str) -> FundNavQuote:
        rows = self._fund_client().get_realtime_increase_rate(fund_code)
        if rows is None or len(rows.index) == 0:
            raise RuntimeError("efinance returned no estimate rows")
        row = rows.iloc[0]
        raw_value = row.get("估算净值") or row.get("单位净值")
        return FundNavQuote(
            value=Decimal(str(raw_value)),
            valuation_date=self._clock().astimezone().date(),
            source=self.source,
            source_url=self.source_url,
            fetched_at=self._clock(),
            quote_type=QuoteType.INTRADAY_ESTIMATE,
        )
