from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Self
from urllib.parse import unquote_plus

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session

from finance_app.config import Settings
from finance_app.db import Base, create_db_engine
from finance_app.notifications.base import DeliveryStatus, Notification
from finance_app.notifications.models import NotificationChannel
from finance_app.notifications.service import NotificationDeliveryService
from finance_app.notifications.wechat import WeChatNotifier, build_wechat_notifier


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TrackingClient:
    def __init__(self, outcome: httpx.Response | Exception) -> None:
        self.outcome = outcome
        self.entered = 0
        self.closed = False
        self.posts = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        self.posts += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def wechat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCE_PUSHPLUS_TOKEN", "pushplus-secret-token")
    monkeypatch.setenv("FINANCE_SERVERCHAN_SENDKEY", "SCTserverchan_secret")
    monkeypatch.setenv(
        "FINANCE_WECOM_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key",
    )


@pytest.mark.parametrize(
    ("channel", "success_payload", "expected_url"),
    [
        ("pushplus", {"code": 200}, "https://www.pushplus.plus/send"),
        (
            "serverchan",
            {"code": 0},
            "https://sctapi.ftqq.com/SCTserverchan_secret.send",
        ),
        (
            "wecom",
            {"errcode": 0},
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=wecom-secret-key",
        ),
    ],
)
def test_wechat_providers_post_to_the_expected_https_endpoint(
    wechat_env: None,
    channel: str,
    success_payload: dict[str, int],
    expected_url: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=success_payload)

    notifier = build_wechat_notifier(
        channel, Settings(), client=make_client(handler)
    )
    result = notifier.send(Notification("Risk alert", "Reserve is below target"))

    assert result.status is DeliveryStatus.SUCCESS
    assert result.provider == channel
    assert result.error_summary is None
    assert requests[0].method == "POST"
    assert str(requests[0].url) == expected_url
    assert requests[0].extensions["timeout"] == {
        "connect": 3.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 3.0,
    }
    assert "Risk alert" in unquote_plus(requests[0].content.decode())


def test_pushplus_accepts_zero_as_a_success_code(
    wechat_env: None,
) -> None:
    notifier = build_wechat_notifier(
        "pushplus",
        Settings(),
        client=make_client(lambda _request: httpx.Response(200, json={"code": 0})),
    )

    assert notifier.send(Notification("Digest", "Body")).status is DeliveryStatus.SUCCESS


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FINANCE_SERVERCHAN_SENDKEY", "https://evil.example/steal"),
        ("FINANCE_SERVERCHAN_SENDKEY", "abc/../../send"),
        (
            "FINANCE_WECOM_WEBHOOK_URL",
            "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret",
        ),
        (
            "FINANCE_WECOM_WEBHOOK_URL",
            "https://evil.example/cgi-bin/webhook/send?key=secret",
        ),
        (
            "FINANCE_WECOM_WEBHOOK_URL",
            "https://qyapi.weixin.qq.com.evil.example/cgi-bin/webhook/send?key=secret",
        ),
        (
            "FINANCE_WECOM_WEBHOOK_URL",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret&url=https://evil.example",
        ),
    ],
)
def test_token_and_webhook_validation_prevents_url_injection_and_ssrf(
    monkeypatch: pytest.MonkeyPatch,
    wechat_env: None,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(400, False), (401, False), (429, False), (503, True)],
)
def test_http_failures_are_normalized_and_do_not_expose_response_or_token(
    wechat_env: None, status_code: int, retryable: bool
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, text="pushplus-secret-token provider detail"
        )

    result = build_wechat_notifier(
        "pushplus", Settings(), client=make_client(handler)
    ).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is retryable
    assert result.error_code == "upstream_http"
    assert str(status_code) in (result.error_summary or "")
    assert "pushplus-secret-token" not in (result.error_summary or "")


@pytest.mark.parametrize("status_code", [204, 302])
def test_any_non_200_http_status_is_normalized_without_raising(
    wechat_env: None, status_code: int
) -> None:
    result = build_wechat_notifier(
        "pushplus",
        Settings(),
        client=make_client(
            lambda _request: httpx.Response(status_code, content=b"provider detail")
        ),
    ).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.error_code == "upstream_http"
    assert result.http_status == status_code
    assert result.retryable is False


@pytest.mark.parametrize(
    ("channel", "secret"),
    [
        ("serverchan", "SCTserverchan_secret"),
        ("wecom", "wecom-secret-key"),
    ],
)
def test_standard_httpx_request_logs_redact_url_credentials(
    wechat_env: None,
    channel: str,
    secret: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    payload = {"code": 0} if channel == "serverchan" else {"errcode": 0}
    notifier = build_wechat_notifier(
        channel,
        Settings(),
        client=make_client(lambda _request: httpx.Response(200, json=payload)),
    )

    notifier.send(Notification("Digest", "Body"))

    assert secret not in caplog.text


@pytest.mark.parametrize("encoded_key", ["%6Bey", "%6bey"])
def test_percent_encoded_wecom_query_name_is_canonicalized_before_request_and_log(
    monkeypatch: pytest.MonkeyPatch,
    encoded_key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "encoded-query-secret"
    monkeypatch.setenv(
        "FINANCE_WECOM_WEBHOOK_URL",
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?{encoded_key}={token}",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errcode": 0})

    caplog.set_level(logging.INFO, logger="httpx")
    result = build_wechat_notifier(
        "wecom", Settings(), client=make_client(handler)
    ).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.SUCCESS
    assert str(requests[0].url) == (
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
        "?key=encoded-query-secret"
    )
    assert token not in caplog.text


@pytest.mark.parametrize(
    "query",
    [
        "key=first-secret&key=second-secret",
        "%6Bey=first-secret&%6bey=second-secret",
        "%6Bey=credential-secret&extra=value",
    ],
)
def test_wecom_rejects_duplicate_or_extra_encoded_query_parameters(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    monkeypatch.setenv(
        "FINANCE_WECOM_WEBHOOK_URL",
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?{query}",
    )

    with pytest.raises(ValidationError) as caught:
        Settings()

    assert "secret" not in str(caught.value)


def test_timeout_is_retryable_and_exception_text_is_redacted(
    wechat_env: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("wecom-secret-key timed out", request=request)

    result = build_wechat_notifier(
        "wecom", Settings(), client=make_client(handler)
    ).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is True
    assert result.error_code == "upstream_timeout"
    assert "wecom-secret-key" not in (result.error_summary or "")


def test_provider_rejection_is_permanent_and_response_is_not_persistable(
    wechat_env: None,
) -> None:
    result = build_wechat_notifier(
        "serverchan",
        Settings(),
        client=make_client(
            lambda _request: httpx.Response(
                200,
                json={"code": 40001, "message": "SCTserverchan_secret invalid"},
            )
        ),
    ).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is False
    assert result.error_code == "provider_rejected"
    assert "SCTserverchan_secret" not in (result.error_summary or "")


@pytest.mark.parametrize(
    ("channel", "payload"),
    [
        ("pushplus", {"code": False}),
        ("pushplus", {"code": 0.0}),
        ("serverchan", {"code": False}),
        ("wecom", {"errcode": False}),
    ],
)
def test_non_integer_provider_codes_are_never_treated_as_success(
    wechat_env: None, channel: str, payload: dict[str, object]
) -> None:
    result = build_wechat_notifier(
        channel,
        Settings(),
        client=make_client(lambda _request: httpx.Response(200, json=payload)),
    ).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.error_code == "provider_rejected"


def test_secret_settings_repr_does_not_expose_tokens(wechat_env: None) -> None:
    rendered = repr(Settings())

    assert "pushplus-secret-token" not in rendered
    assert "SCTserverchan_secret" not in rendered
    assert "wecom-secret-key" not in rendered


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("FINANCE_PUSHPLUS_TOKEN", "pushplus-credential secret"),
        ("FINANCE_SERVERCHAN_SENDKEY", "https://evil.example/credential-secret"),
        (
            "FINANCE_WECOM_WEBHOOK_URL",
            "https://evil.example/cgi-bin/webhook/send?key=credential-secret",
        ),
    ],
)
def test_invalid_wechat_secret_is_hidden_from_validation_error(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    secret: str,
) -> None:
    monkeypatch.setenv(name, secret)

    with pytest.raises(ValidationError) as caught:
        Settings()

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_missing_token_is_a_permanent_configuration_failure() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    result = build_wechat_notifier(
        "pushplus",
        settings,
        client=make_client(lambda _request: pytest.fail("must not send")),
    ).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is False
    assert result.error_code == "invalid_configuration"


def test_unknown_wechat_provider_is_rejected_without_network() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="unsupported WeChat provider"):
        build_wechat_notifier("https://evil.example/token", settings)


def test_owned_http_client_is_closed_after_success(
    monkeypatch: pytest.MonkeyPatch, wechat_env: None
) -> None:
    clients: list[TrackingClient] = []

    def client_factory() -> TrackingClient:
        client = TrackingClient(httpx.Response(200, json={"code": 200}))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", client_factory)
    notifier = build_wechat_notifier("pushplus", Settings())

    result = notifier.send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.SUCCESS
    assert len(clients) == 1
    assert clients[0].entered == 1
    assert clients[0].closed is True


def test_owned_http_client_is_closed_after_failure(
    monkeypatch: pytest.MonkeyPatch, wechat_env: None
) -> None:
    clients: list[TrackingClient] = []

    def client_factory() -> TrackingClient:
        request = httpx.Request("POST", "https://www.pushplus.plus/send")
        client = TrackingClient(httpx.ReadTimeout("secret", request=request))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", client_factory)
    notifier = build_wechat_notifier("pushplus", Settings())

    result = notifier.send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is True
    assert len(clients) == 1
    assert clients[0].closed is True


def test_owned_http_client_is_closed_for_each_service_retry(
    monkeypatch: pytest.MonkeyPatch, wechat_env: None
) -> None:
    clients: list[TrackingClient] = []

    def client_factory() -> TrackingClient:
        client = TrackingClient(httpx.Response(503, text="secret"))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", client_factory)
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        channel = NotificationChannel(name="PushPlus", channel_type="pushplus")
        session.add(channel)
        session.flush()
        result = NotificationDeliveryService(
            session, sleep=lambda _seconds: None
        ).deliver(
            channel,
            build_wechat_notifier("pushplus", Settings()),
            Notification("Digest", "Body"),
        )

    assert result.status is DeliveryStatus.FAILED
    assert result.attempt_count == 3
    assert len(clients) == 3
    assert all(client.closed for client in clients)


def test_injected_http_client_is_never_closed(wechat_env: None) -> None:
    client = TrackingClient(httpx.Response(200, json={"code": 200}))
    notifier = build_wechat_notifier("pushplus", Settings(), client=client)  # type: ignore[arg-type]

    result = notifier.send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.SUCCESS
    assert client.posts == 1
    assert client.entered == 0
    assert client.closed is False


@pytest.mark.parametrize(
    ("provider", "credential"),
    [
        ("unknown", "https://evil.example/token"),
        ("pushplus", ""),
        ("pushplus", "credential token"),
        ("pushplus", "x" * 513),
        ("wecom", "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"),
        ("wecom", "https://evil.example/cgi-bin/webhook/send?key=secret"),
        ("serverchan", "https://evil.example/token"),
    ],
)
def test_direct_notifier_construction_cannot_bypass_ssrf_validation(
    provider: str, credential: str
) -> None:
    with pytest.raises(ValueError):
        WeChatNotifier(provider=provider, credential=SecretStr(credential))
