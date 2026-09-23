from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from finance_app.db import utc_now
from finance_app.market.base import FundNavQuote, MarketDataError

EASTMONEY_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
EASTMONEY_REFERER = "https://fundf10.eastmoney.com/"
TOTAL_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 1_000_000
_FUND_CODE = re.compile(r"[0-9]{6}\Z", re.ASCII)
_NAV = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)


class EastMoneyFundNavProvider:
    source = "eastmoney"
    source_url = EASTMONEY_NAV_URL

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = client if client is not None else httpx.Client()
        self._clock = clock
        self._timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)

    def fetch(self, fund_code: str) -> FundNavQuote:
        if not isinstance(fund_code, str) or _FUND_CODE.fullmatch(fund_code) is None:
            raise ValueError("fund code must contain exactly six ASCII digits")

        params = {"fundCode": fund_code, "pageIndex": "1", "pageSize": "1"}
        request_url = str(httpx.URL(self.source_url, params=params))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
            ),
            "Referer": EASTMONEY_REFERER,
            "Accept": "application/json, text/plain, */*",
        }

        for attempt in range(1, TOTAL_ATTEMPTS + 1):
            try:
                response = self._client.get(
                    self.source_url,
                    params=params,
                    headers=headers,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException:
                if attempt < TOTAL_ATTEMPTS:
                    continue
                raise self._error(
                    "upstream_timeout",
                    request_url,
                    attempt,
                    f"upstream_timeout: eastmoney request failed after {attempt} attempts",
                ) from None
            except httpx.RequestError:
                if attempt < TOTAL_ATTEMPTS:
                    continue
                raise self._error(
                    "upstream_network",
                    request_url,
                    attempt,
                    f"upstream_network: eastmoney request failed after {attempt} attempts",
                ) from None

            if response.status_code != httpx.codes.OK:
                retryable = response.status_code == 429 or response.status_code >= 500
                if retryable and attempt < TOTAL_ATTEMPTS:
                    continue
                raise self._error(
                    "upstream_http",
                    str(response.request.url),
                    attempt,
                    f"upstream_http: eastmoney returned HTTP {response.status_code}",
                )

            try:
                quote_date, value = self._parse_response(response)
            except (ValueError, TypeError, InvalidOperation):
                raise self._error(
                    "invalid_response",
                    str(response.request.url),
                    attempt,
                    "invalid_response: eastmoney returned an invalid daily NAV payload",
                ) from None
            return FundNavQuote(
                value=value,
                valuation_date=quote_date,
                source=self.source,
                source_url=str(response.request.url),
                fetched_at=self._clock(),
                attempts=attempt,
            )

        raise AssertionError("bounded retry loop exited unexpectedly")

    def _parse_response(self, response: httpx.Response) -> tuple[date, Decimal]:
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds size limit")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("response must be an object")
        data = payload.get("Data")
        if not isinstance(data, Mapping):
            raise TypeError("Data must be an object")
        rows = data.get("LSJZList")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
            raise ValueError("LSJZList must contain a row")
        raw_date = rows[0].get("FSRQ")
        raw_value = rows[0].get("DWJZ")
        if not isinstance(raw_date, str):
            raise TypeError("FSRQ must be a string")
        quote_date = date.fromisoformat(raw_date)
        if quote_date.isoformat() != raw_date:
            raise ValueError("FSRQ must use YYYY-MM-DD")
        if not isinstance(raw_value, str) or _NAV.fullmatch(raw_value) is None:
            raise ValueError("DWJZ must be a decimal string")
        value = Decimal(raw_value)
        if not value.is_finite() or value <= 0:
            raise ValueError("DWJZ must be finite and positive")
        return quote_date, value

    def _error(
        self, code: str, source_url: str, attempts: int, summary: str
    ) -> MarketDataError:
        return MarketDataError(
            code=code,
            source=self.source,
            source_url=source_url,
            fetched_at=self._clock(),
            attempts=attempts,
            summary=summary,
        )
