from __future__ import annotations

import gzip
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from finance_app.market.base import MarketDataError
from finance_app.market.eastmoney import MAX_RESPONSE_BYTES, EastMoneyFundNavProvider

NOW = datetime(2026, 9, 23, 1, 2, 3, tzinfo=UTC)


def provider(handler) -> EastMoneyFundNavProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return EastMoneyFundNavProvider(client=client, clock=lambda: NOW)


class CountingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_read = 0
        self.closed = False

    def __iter__(self):
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_eastmoney_maps_daily_nav_and_request_provenance():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": "1.2345"}]}},
        )

    quote = provider(handler).fetch("000001")

    assert quote.value == Decimal("1.2345")
    assert quote.valuation_date == date(2026, 9, 22)
    assert quote.source == "eastmoney"
    assert quote.fetched_at == NOW
    assert quote.source_url == str(requests[0].url)
    assert dict(requests[0].url.params) == {
        "fundCode": "000001",
        "pageIndex": "1",
        "pageSize": "1",
    }
    assert requests[0].headers["referer"] == "https://fundf10.eastmoney.com/"
    assert "Mozilla/5.0" in requests[0].headers["user-agent"]
    assert requests[0].extensions["timeout"] == {
        "connect": 3.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 3.0,
    }


def test_timeout_gets_two_retries_for_three_total_attempts():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("upstream included secret-token", request=request)

    with pytest.raises(MarketDataError) as caught:
        provider(handler).fetch("000001")

    assert attempts == 3
    assert caught.value.code == "upstream_timeout"
    assert caught.value.attempts == 3
    assert caught.value.fetched_at == NOW
    assert "secret-token" not in caught.value.summary


def test_retry_can_succeed_on_the_third_total_attempt():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(
            200,
            json={"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": "1.2"}]}},
        )

    quote = provider(handler).fetch("000001")
    assert quote.value == Decimal("1.2")
    assert quote.attempts == 3
    assert attempts == 3


@pytest.mark.parametrize("code", ["", "1", "00001a", " 000001", "000001 ", "0000001"])
def test_invalid_fund_code_is_rejected_without_a_request(code: str):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json={})

    with pytest.raises(ValueError, match="six ASCII digits"):
        provider(handler).fetch(code)
    assert attempts == 0


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"Data": None},
        {"Data": {"LSJZList": None}},
        {"Data": {"LSJZList": []}},
        {"Data": {"LSJZList": [None]}},
        {"Data": {"LSJZList": [{}]}},
        {"Data": {"LSJZList": [{"FSRQ": "09/22/2026", "DWJZ": "1.2"}]}},
        {"Data": {"LSJZList": [{"FSRQ": "2026-02-30", "DWJZ": "1.2"}]}},
        {"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": 1.2}]}},
        {"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": "NaN"}]}},
        {"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": "Infinity"}]}},
        {"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": "0"}]}},
        {"Data": {"LSJZList": [{"FSRQ": "2026-09-22", "DWJZ": "-1"}]}},
    ],
)
def test_untrusted_json_shape_date_and_nav_are_rejected_without_retry(payload):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if payload is None:
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(200, json=payload)

    with pytest.raises(MarketDataError) as caught:
        provider(handler).fetch("000001")

    assert attempts == 1
    assert caught.value.code == "invalid_response"
    assert "html" not in caught.value.summary.lower()


def test_http_error_summary_does_not_echo_response_body_and_retries_5xx():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="password=do-not-leak")

    with pytest.raises(MarketDataError) as caught:
        provider(handler).fetch("000001")

    assert attempts == 3
    assert caught.value.code == "upstream_http"
    assert "503" in caught.value.summary
    assert "do-not-leak" not in caught.value.summary


def test_decompressed_size_limit_closes_stream_without_consuming_later_chunks():
    stream = CountingStream(
        [
            gzip.compress(b"x" * (MAX_RESPONSE_BYTES + 1)),
            gzip.compress(b"must-not-be-consumed"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=stream,
        )

    with pytest.raises(MarketDataError) as caught:
        provider(handler).fetch("000001")

    assert caught.value.code == "invalid_response"
    assert stream.chunks_read == 1
    assert stream.closed is True
