from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from finance_app.config import (
    Settings,
    validate_opaque_secret_value,
    validate_serverchan_sendkey_value,
    validate_wecom_webhook_url_value,
)
from finance_app.notifications.base import DeliveryResult, Notification

PUSHPLUS_URL = "https://www.pushplus.plus/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/{sendkey}.send"
TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)
_SERVERCHAN_LOG_URL = re.compile(
    r"(https://sctapi\.ftqq\.com/)[^/?#\s]+(\.send)", re.ASCII
)
_WECOM_LOG_KEY = re.compile(r"([?&]key=)[^&#\s]+", re.ASCII)


def _redact_httpx_log_value(value: object) -> object:
    rendered = str(value)
    redacted = _SERVERCHAN_LOG_URL.sub(r"\1<redacted>\2", rendered)
    redacted = _WECOM_LOG_KEY.sub(r"\1<redacted>", redacted)
    return redacted if redacted != rendered else value


class _CredentialUrlFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_httpx_log_value(item) for item in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact_httpx_log_value(item)
                for key, item in record.args.items()
            }
        record.msg = _redact_httpx_log_value(record.msg)
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(item, _CredentialUrlFilter) for item in _httpx_logger.filters):
    _httpx_logger.addFilter(_CredentialUrlFilter())


class WeChatNotifier:
    def __init__(
        self,
        *,
        provider: str,
        credential: SecretStr | None,
        client: httpx.Client | None = None,
    ) -> None:
        if provider not in {"pushplus", "serverchan", "wecom"}:
            raise ValueError(f"unsupported WeChat provider: {provider!r}")
        if credential is not None:
            raw = credential.get_secret_value()
            if provider == "pushplus":
                validate_opaque_secret_value(raw)
            elif provider == "serverchan":
                validate_serverchan_sendkey_value(raw)
            elif provider == "wecom":
                credential = SecretStr(validate_wecom_webhook_url_value(raw))
        self.provider = provider
        self._credential = credential
        self._client = client if client is not None else httpx.Client()

    def send(self, notification: Notification) -> DeliveryResult:
        if self._credential is None:
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="invalid_configuration",
                error_summary=(
                    f"invalid_configuration: {self.provider} channel is incomplete"
                ),
                retryable=False,
            )

        try:
            response = self._post(notification, self._credential.get_secret_value())
        except httpx.TimeoutException:
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="upstream_timeout",
                error_summary=f"upstream_timeout: {self.provider} request timed out",
                retryable=True,
            )
        except httpx.RequestError:
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="upstream_network",
                error_summary=f"upstream_network: {self.provider} request failed",
                retryable=True,
            )

        if response.status_code != httpx.codes.OK:
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="upstream_http",
                error_summary=(
                    f"upstream_http: {self.provider} returned HTTP "
                    f"{response.status_code}"
                ),
                retryable=response.status_code >= 500,
                http_status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError:
            return self._invalid_response()
        if not isinstance(payload, Mapping):
            return self._invalid_response()
        if self._is_success(payload):
            return DeliveryResult.success(provider=self.provider)
        return DeliveryResult.failed(
            provider=self.provider,
            error_code="provider_rejected",
            error_summary=f"provider_rejected: {self.provider} rejected delivery",
            retryable=False,
        )

    def _post(self, notification: Notification, credential: str) -> httpx.Response:
        if self.provider == "pushplus":
            return self._client.post(
                PUSHPLUS_URL,
                json={
                    "token": credential,
                    "title": notification.title,
                    "content": notification.body,
                    "template": "txt",
                },
                timeout=TIMEOUT,
            )
        if self.provider == "serverchan":
            return self._client.post(
                SERVERCHAN_URL.format(sendkey=credential),
                data={"title": notification.title, "desp": notification.body},
                timeout=TIMEOUT,
            )
        return self._client.post(
            credential,
            json={
                "msgtype": "text",
                "text": {"content": f"{notification.title}\n\n{notification.body}"},
            },
            timeout=TIMEOUT,
        )

    def _is_success(self, payload: Mapping[str, Any]) -> bool:
        if self.provider == "pushplus":
            code = payload.get("code")
            return type(code) is int and code in (0, 200)
        if self.provider == "serverchan":
            code = payload.get("code")
            return type(code) is int and code == 0
        code = payload.get("errcode")
        return type(code) is int and code == 0

    def _invalid_response(self) -> DeliveryResult:
        return DeliveryResult.failed(
            provider=self.provider,
            error_code="invalid_response",
            error_summary=f"invalid_response: {self.provider} returned invalid data",
            retryable=False,
        )


def build_wechat_notifier(
    channel: str,
    settings: Settings,
    *,
    client: httpx.Client | None = None,
) -> WeChatNotifier:
    credentials = {
        "pushplus": settings.pushplus_token,
        "serverchan": settings.serverchan_sendkey,
        "wecom": settings.wecom_webhook_url,
    }
    if channel not in credentials:
        raise ValueError(f"unsupported WeChat provider: {channel!r}")
    return WeChatNotifier(
        provider=channel, credential=credentials[channel], client=client
    )
