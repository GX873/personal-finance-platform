"""Strict adapter for Tiantian Fund's intraday estimate JSONP endpoint."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Self
from zoneinfo import ZoneInfo

import httpx

from finance_app.db import utc_now
from finance_app.market.base import (
    FundNavQuote,
    MarketDataError,
    QuoteType,
    validate_storable_nav,
)

TIANTIAN_ESTIMATE_URL = "https://fundgz.1234567.com.cn/"
MAX_RESPONSE_BYTES = 64 * 1024
TOTAL_ATTEMPTS = 3
_FUND_CODE = re.compile(r"[0-9]{6}\Z", re.ASCII)
_JSONP = re.compile(r"\Ajsonpgz\((\{.*\})\);\Z", re.DOTALL)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class TiantianEstimateProvider:
    source = "tiantian:estimate"
    source_url = TIANTIAN_ESTIMATE_URL

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=5.0, follow_redirects=False)
        self._clock = clock
        self._timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def fetch(self, fund_code: str) -> FundNavQuote:
        if not isinstance(fund_code, str) or _FUND_CODE.fullmatch(fund_code) is None:
            raise ValueError("fund code must contain six digits")

        url = f"{TIANTIAN_ESTIMATE_URL}js/{fund_code}.js"
        payload = self._request_and_parse(url)
        if payload.get("fundcode") != fund_code:
            raise self._error(
                "fund_code_mismatch",
                url,
                self._last_attempts,
                "estimate fund code did not match",
            )

        try:
            raw_timestamp = payload["gztime"]
            if not isinstance(raw_timestamp, str):
                raise TypeError
            observed_at = datetime.strptime(raw_timestamp, "%Y-%m-%d %H:%M").replace(
                tzinfo=SHANGHAI
            )
            if observed_at.strftime("%Y-%m-%d %H:%M") != raw_timestamp:
                raise ValueError
            raw_value = payload["gsz"]
            if not isinstance(raw_value, str):
                raise TypeError
            value = Decimal(raw_value)
            if not value.is_finite() or value <= 0:
                raise ValueError
            validate_storable_nav(value)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise self._error(
                "invalid_response",
                url,
                self._last_attempts,
                "invalid_response: tiantian returned an invalid estimate payload",
            ) from None

        return FundNavQuote(
            value=value,
            valuation_date=observed_at.date(),
            source=self.source,
            source_url=url,
            fetched_at=self._clock(),
            attempts=self._last_attempts,
            quote_type=QuoteType.INTRADAY_ESTIMATE,
        )

    def _request_and_parse(self, url: str) -> Mapping[str, object]:
        self._last_attempts = 1
        for attempt in range(1, TOTAL_ATTEMPTS + 1):
            self._last_attempts = attempt
            try:
                response = self._client.get(
                    url,
                    timeout=self._timeout,
                    headers={
                        "Accept": "application/javascript, text/javascript, */*;q=0.1",
                        "Referer": "https://fund.eastmoney.com/",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
                        ),
                    },
                )
                status = response.status_code
                if status != httpx.codes.OK:
                    retryable = status == 429 or status >= 500
                    if retryable and attempt < TOTAL_ATTEMPTS:
                        continue
                    raise self._error(
                        "upstream_http",
                        url,
                        attempt,
                        f"upstream_http: tiantian returned HTTP {status}",
                    )
                response.raise_for_status()
                content = response.content
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds size limit")
                match = _JSONP.fullmatch(content.decode("utf-8"))
                if match is None:
                    raise ValueError("invalid JSONP wrapper")
                payload = json.loads(match.group(1))
                if not isinstance(payload, Mapping):
                    raise TypeError("payload must be an object")
                return payload
            except MarketDataError:
                raise
            except httpx.TimeoutException:
                if attempt < TOTAL_ATTEMPTS:
                    continue
                raise self._error(
                    "upstream_timeout",
                    url,
                    attempt,
                    f"upstream_timeout: tiantian request failed after {attempt} attempts",
                ) from None
            except httpx.RequestError:
                if attempt < TOTAL_ATTEMPTS:
                    continue
                raise self._error(
                    "upstream_network",
                    url,
                    attempt,
                    f"upstream_network: tiantian request failed after {attempt} attempts",
                ) from None
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                raise self._error(
                    "invalid_response",
                    url,
                    attempt,
                    "invalid_response: tiantian returned an invalid estimate payload",
                ) from None

        raise AssertionError("bounded retry loop exited unexpectedly")

    def _error(
        self,
        code: str,
        url: str,
        attempts: int,
        summary: str,
    ) -> MarketDataError:
        return MarketDataError(
            code=code,
            source=self.source,
            source_url=url,
            fetched_at=self._clock(),
            attempts=attempts,
            summary=summary,
        )
