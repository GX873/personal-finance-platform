from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from finance_app.db import utc_now
from finance_app.notifications.base import (
    DeliveryResult,
    DeliveryStatus,
    Notification,
    NotificationAdapter,
)
from finance_app.notifications.models import NotificationChannel, NotificationDelivery

TOTAL_ATTEMPTS = 3
RETRY_DELAYS = (1.0, 3.0)
_SAFE_ERROR_SUMMARIES = {
    "authentication_failed": "authentication_failed: provider rejected credentials",
    "delivery_failed": "delivery_failed: provider delivery failed",
    "invalid_configuration": "invalid_configuration: channel is incomplete",
    "invalid_response": "invalid_response: provider returned invalid data",
    "provider_rejected": "provider_rejected: provider rejected delivery",
    "recipient_rejected": "recipient_rejected: provider rejected the recipient",
    "temporary_failure": "temporary_failure: provider is temporarily unavailable",
    "transport_error": "transport_error: provider transport failed",
    "upstream_http": "upstream_http: provider returned an HTTP error",
    "upstream_network": "upstream_network: provider request failed",
    "upstream_timeout": "upstream_timeout: provider request timed out",
}
_PERMANENT_ERRORS = {
    "authentication_failed",
    "delivery_failed",
    "invalid_configuration",
    "invalid_response",
    "provider_rejected",
    "recipient_rejected",
}
_TRANSIENT_ERRORS = {
    "temporary_failure",
    "transport_error",
    "upstream_network",
    "upstream_timeout",
}


@dataclass(frozen=True)
class DeliveryBatchResult:
    status: DeliveryStatus
    results: Mapping[str, DeliveryResult]


class NotificationDeliveryService:
    """Send with bounded retries while the caller owns the transaction."""

    def __init__(
        self,
        session: Session,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._sleep = sleep
        self._clock = clock

    def deliver(
        self,
        channel: NotificationChannel,
        notifier: NotificationAdapter,
        notification: Notification,
    ) -> DeliveryResult:
        if channel.id is None:
            self._session.add(channel)
            self._session.flush()
        assert channel.id is not None

        delivery = NotificationDelivery(
            channel_id=channel.id,
            title=notification.title,
            status="pending",
            attempt_count=0,
            error_summary=None,
            created_at=self._clock(),
        )
        self._session.add(delivery)
        self._session.flush()

        for attempt in range(1, TOTAL_ATTEMPTS + 1):
            result = self._send_once(notifier, notification)
            result = self._sanitize(result).with_attempt_count(attempt)
            delivery.attempt_count = attempt
            delivery.error_summary = result.error_summary

            if result.status is DeliveryStatus.SUCCESS:
                delivery.status = DeliveryStatus.SUCCESS.value
                delivery.delivered_at = self._clock()
                self._session.flush()
                return result

            retrying = result.retryable and attempt < TOTAL_ATTEMPTS
            delivery.status = "retrying" if retrying else DeliveryStatus.FAILED.value
            self._session.flush()
            if not retrying:
                return result
            self._sleep(RETRY_DELAYS[attempt - 1])

        raise AssertionError("bounded retry loop exited unexpectedly")

    def deliver_channels(
        self,
        notification: Notification,
        channels: Mapping[
            str, tuple[NotificationChannel, NotificationAdapter]
        ],
        *,
        primary_channel: str = "email",
    ) -> DeliveryBatchResult:
        if primary_channel not in channels:
            raise ValueError("primary channel is missing")
        results = {
            name: self.deliver(channel, notifier, notification)
            for name, (channel, notifier) in channels.items()
        }
        return DeliveryBatchResult(
            status=results[primary_channel].status,
            results=results,
        )

    def _send_once(
        self, notifier: NotificationAdapter, notification: Notification
    ) -> DeliveryResult:
        try:
            return notifier.send(notification)
        except httpx.TimeoutException:
            return DeliveryResult.failed(
                provider=notifier.provider,
                error_code="upstream_timeout",
                error_summary="upstream_timeout: provider request timed out",
                retryable=True,
            )
        except httpx.RequestError:
            return DeliveryResult.failed(
                provider=notifier.provider,
                error_code="upstream_network",
                error_summary="upstream_network: provider request failed",
                retryable=True,
            )
        except (TimeoutError, ConnectionError, OSError):
            return DeliveryResult.failed(
                provider=notifier.provider,
                error_code="transport_error",
                error_summary="transport_error: provider transport failed",
                retryable=True,
            )
        except (TypeError, ValueError):
            return DeliveryResult.failed(
                provider=notifier.provider,
                error_code="invalid_configuration",
                error_summary="invalid_configuration: channel is incomplete",
                retryable=False,
            )
        except Exception:  # noqa: BLE001
            # Adapter exceptions cross a credential boundary, so never persist details.
            return DeliveryResult.failed(
                provider=notifier.provider,
                error_code="delivery_failed",
                error_summary="delivery_failed: provider delivery failed",
                retryable=False,
            )

    def _sanitize(self, result: DeliveryResult) -> DeliveryResult:
        if result.status is DeliveryStatus.SUCCESS:
            return result
        code = result.error_code or "delivery_failed"
        summary = _SAFE_ERROR_SUMMARIES.get(
            code, _SAFE_ERROR_SUMMARIES["delivery_failed"]
        )
        if code not in _SAFE_ERROR_SUMMARIES:
            code = "delivery_failed"
        if code == "upstream_http":
            retryable = result.http_status is not None and result.http_status >= 500
            summary = (
                f"upstream_http: provider returned HTTP {result.http_status}"
                if result.http_status is not None
                else summary
            )
        elif code in _TRANSIENT_ERRORS:
            retryable = True
        else:
            assert code in _PERMANENT_ERRORS
            retryable = False
        return DeliveryResult.failed(
            provider=result.provider,
            error_code=code,
            error_summary=summary,
            retryable=retryable,
            http_status=result.http_status if code == "upstream_http" else None,
        )
