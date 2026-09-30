from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from finance_app.market.base import MarketDataError, QuoteType
from finance_app.market.tiantian_estimate import TiantianEstimateProvider

NOW = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)


def response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        text=body,
        request=httpx.Request("GET", "https://fundgz.1234567.com.cn/js/000001.js"),
    )


class Client:
    def __init__(self, result: httpx.Response):
        self.result = result
        self.calls = 0

    def get(self, url: str, **kwargs) -> httpx.Response:
        self.calls += 1
        assert url == "https://fundgz.1234567.com.cn/js/000001.js"
        return self.result


def test_maps_jsonp_to_intraday_quote():
    body = (
        'jsonpgz({"fundcode":"000001","name":"测试基金",'
        '"jzrq":"2026-09-28","dwjz":"1.20","gsz":"1.2345",'
        '"gszzl":"2.88","gztime":"2026-09-29 14:05"});'
    )
    quote = TiantianEstimateProvider(Client(response(body)), clock=lambda: NOW).fetch(
        "000001"
    )
    assert quote.value == Decimal("1.2345")
    assert quote.valuation_date == date(2026, 9, 29)
    assert quote.quote_type is QuoteType.INTRADAY_ESTIMATE
    assert quote.source == "tiantian:estimate"
    assert quote.source_url == "https://fundgz.1234567.com.cn/js/000001.js"
    assert quote.fetched_at == NOW


@pytest.mark.parametrize(
    "body",
    [
        "",
        "jsonpgz();",
        "jsonpgz({bad});",
        'jsonpgz({"fundcode":"999999","gsz":"1.2","gztime":"2026-09-29 14:05"});',
        'jsonpgz({"fundcode":"000001","gsz":"0","gztime":"2026-09-29 14:05"});',
        'jsonpgz({"fundcode":"000001","gsz":"1.2","gztime":"2026-09-29"});',
        'jsonpgz({"fundcode":"000001","gsz":"1.2","gztime":"2026-09-29 14:05"})',
        'jsonpgz({"fundcode":"000001","gsz":"1.2","gztime":"2026-09-29 14:05"});evil',
    ],
)
def test_rejects_unusable_jsonp(body: str):
    with pytest.raises(MarketDataError) as caught:
        TiantianEstimateProvider(Client(response(body)), clock=lambda: NOW).fetch(
            "000001"
        )
    assert caught.value.code in {"invalid_response", "fund_code_mismatch"}
    if body:
        assert body not in caught.value.summary


def test_retries_transport_failures_three_times_and_sanitizes_error():
    class FailingClient(Client):
        def get(self, url: str, **kwargs) -> httpx.Response:
            self.calls += 1
            raise httpx.ReadTimeout("secret-token", request=httpx.Request("GET", url))

    client = FailingClient(response(""))
    with pytest.raises(MarketDataError) as caught:
        TiantianEstimateProvider(client, clock=lambda: NOW).fetch("000001")
    assert client.calls == 3
    assert caught.value.code == "upstream_timeout"
    assert caught.value.attempts == 3
    assert "secret-token" not in caught.value.summary


def test_retries_server_failures_but_does_not_retry_client_errors():
    client = Client(response("upstream detail", status=503))
    with pytest.raises(MarketDataError) as caught:
        TiantianEstimateProvider(client, clock=lambda: NOW).fetch("000001")
    assert client.calls == 3
    assert caught.value.code == "upstream_http"
    assert "upstream detail" not in caught.value.summary

    client = Client(response("bad request", status=400))
    with pytest.raises(MarketDataError):
        TiantianEstimateProvider(client, clock=lambda: NOW).fetch("000001")
    assert client.calls == 1


@pytest.mark.parametrize("code", ["", "1", "00001a", " 000001", "000001 ", "0000001"])
def test_invalid_fund_code_is_rejected_without_request(code: str):
    client = Client(response(""))
    with pytest.raises(ValueError, match="six digits"):
        TiantianEstimateProvider(client, clock=lambda: NOW).fetch(code)
    assert client.calls == 0


def test_response_size_is_bounded():
    client = Client(response("x" * (64 * 1024 + 1)))
    with pytest.raises(MarketDataError) as caught:
        TiantianEstimateProvider(client, clock=lambda: NOW).fetch("000001")
    assert caught.value.code == "invalid_response"


def test_rejects_nav_outside_storable_decimal_boundary():
    body = (
        'jsonpgz({"fundcode":"000001","gsz":"1.123456789",'
        '"gztime":"2026-09-29 14:05"});'
    )
    with pytest.raises(MarketDataError) as caught:
        TiantianEstimateProvider(Client(response(body)), clock=lambda: NOW).fetch(
            "000001"
        )
    assert caught.value.code == "invalid_response"


def test_context_closes_only_owned_client():
    with TiantianEstimateProvider(clock=lambda: NOW) as provider:
        owned = provider._client
        assert owned.is_closed is False
    assert owned.is_closed is True

    shared = httpx.Client(transport=httpx.MockTransport(lambda request: response("")))
    with TiantianEstimateProvider(shared, clock=lambda: NOW):
        pass
    assert shared.is_closed is False
    shared.close()
