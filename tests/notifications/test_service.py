from __future__ import annotations

from datetime import UTC, datetime
from itertools import chain

import httpx
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from finance_app.db import Base, create_db_engine
from finance_app.notifications.base import (
    DeliveryResult,
    DeliveryStatus,
    Notification,
)
from finance_app.notifications.models import NotificationChannel, NotificationDelivery
from finance_app.notifications.service import NotificationDeliveryService

NOW = datetime(2026, 9, 24, 1, 2, 3, tzinfo=UTC)


class StubNotifier:
    def __init__(self, provider: str, outcomes: list[DeliveryResult | Exception]):
        self.provider = provider
        self._outcomes = outcomes
        self.calls = 0

    def send(self, notification: Notification) -> DeliveryResult:
        assert notification.title == "Digest"
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def failed(provider: str, *, retryable: bool) -> DeliveryResult:
    return DeliveryResult.failed(
        provider=provider,
        error_code="temporary_failure" if retryable else "authentication_failed",
        error_summary=(
            "temporary_failure: provider is temporarily unavailable"
            if retryable
            else "authentication_failed: provider rejected credentials"
        ),
        retryable=retryable,
    )


def setup_session() -> Session:
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def add_channel(session: Session, name: str, channel_type: str) -> NotificationChannel:
    channel = NotificationChannel(name=name, channel_type=channel_type)
    session.add(channel)
    session.flush()
    return channel


def test_delivery_retries_three_total_attempts_with_one_and_three_second_delays() -> None:
    with setup_session() as session:
        channel = add_channel(session, "Primary email", "email")
        sleeps: list[float] = []
        snapshots: list[tuple[str, int]] = []

        @event.listens_for(session, "after_flush")
        def capture_delivery_state(_session: Session, _context: object) -> None:
            for item in chain(session.new, session.identity_map.values()):
                if isinstance(item, NotificationDelivery):
                    snapshots.append((item.status, item.attempt_count))

        notifier = StubNotifier(
            "email",
            [failed("email", retryable=True) for _ in range(3)],
        )
        result = NotificationDeliveryService(
            session, sleep=sleeps.append, clock=lambda: NOW
        ).deliver(channel, notifier, Notification("Digest", "Body"))

        row = session.scalar(select(NotificationDelivery))
        assert result.status is DeliveryStatus.FAILED
        assert result.attempt_count == 3
        assert notifier.calls == 3
        assert sleeps == [1.0, 3.0]
        assert snapshots == [
            ("pending", 0),
            ("retrying", 1),
            ("retrying", 2),
            ("failed", 3),
        ]
        assert row is not None
        assert row.channel_id == channel.id
        assert row.title == "Digest"
        assert row.status == "failed"
        assert row.attempt_count == 3
        assert row.error_summary == result.error_summary
        assert row.delivered_at is None


def test_permanent_failure_is_not_retried() -> None:
    with setup_session() as session:
        channel = add_channel(session, "Primary email", "email")
        notifier = StubNotifier("email", [failed("email", retryable=False)])
        sleeps: list[float] = []

        result = NotificationDeliveryService(session, sleep=sleeps.append).deliver(
            channel, notifier, Notification("Digest", "Body")
        )

        assert result.status is DeliveryStatus.FAILED
        assert result.attempt_count == 1
        assert notifier.calls == 1
        assert sleeps == []


def test_service_enforces_permanent_error_classification() -> None:
    with setup_session() as session:
        channel = add_channel(session, "Primary email", "email")
        forged_auth_failure = DeliveryResult.failed(
            provider="email",
            error_code="authentication_failed",
            error_summary="authentication_failed: untrusted adapter text",
            retryable=True,
        )
        notifier = StubNotifier("email", [forged_auth_failure])
        sleeps: list[float] = []

        result = NotificationDeliveryService(session, sleep=sleeps.append).deliver(
            channel, notifier, Notification("Digest", "Body")
        )

        assert result.retryable is False
        assert result.attempt_count == 1
        assert notifier.calls == 1
        assert sleeps == []


def test_service_uses_http_status_not_adapter_claim_for_retry_policy() -> None:
    with setup_session() as session:
        channel = add_channel(session, "WeChat backup", "pushplus")
        forged_4xx = DeliveryResult.failed(
            provider="pushplus",
            error_code="upstream_http",
            error_summary="upstream_http: untrusted adapter text",
            retryable=True,
            http_status=401,
        )
        notifier = StubNotifier("pushplus", [forged_4xx])

        result = NotificationDeliveryService(session).deliver(
            channel, notifier, Notification("Digest", "Body")
        )

        assert result.retryable is False
        assert result.attempt_count == 1
        assert notifier.calls == 1


def test_service_retries_5xx_even_when_adapter_claims_it_is_permanent() -> None:
    with setup_session() as session:
        channel = add_channel(session, "WeChat backup", "pushplus")
        forged_5xx = DeliveryResult.failed(
            provider="pushplus",
            error_code="upstream_http",
            error_summary="upstream_http: untrusted adapter text",
            retryable=False,
            http_status=503,
        )
        notifier = StubNotifier(
            "pushplus",
            [forged_5xx, DeliveryResult.success(provider="pushplus")],
        )
        sleeps: list[float] = []

        result = NotificationDeliveryService(session, sleep=sleeps.append).deliver(
            channel, notifier, Notification("Digest", "Body")
        )

        assert result.status is DeliveryStatus.SUCCESS
        assert result.attempt_count == 2
        assert notifier.calls == 2
        assert sleeps == [1.0]


def test_generic_httpx_timeout_is_safely_retried() -> None:
    with setup_session() as session:
        channel = add_channel(session, "WeChat backup", "pushplus")
        notifier = StubNotifier(
            "pushplus",
            [
                httpx.ReadTimeout("credential-do-not-store")
                for _ in range(3)
            ],
        )

        result = NotificationDeliveryService(
            session, sleep=lambda _seconds: None
        ).deliver(channel, notifier, Notification("Digest", "Body"))

        assert result.error_code == "upstream_timeout"
        assert result.retryable is True
        assert result.attempt_count == 3
        assert notifier.calls == 3
        assert "credential-do-not-store" not in (result.error_summary or "")


def test_unexpected_timeout_is_safely_retried_without_exception_text() -> None:
    with setup_session() as session:
        channel = add_channel(session, "Primary email", "email")
        notifier = StubNotifier(
            "email",
            [TimeoutError("credential-do-not-store") for _ in range(3)],
        )

        result = NotificationDeliveryService(
            session, sleep=lambda _seconds: None
        ).deliver(channel, notifier, Notification("Digest", "Body"))

        row = session.scalar(select(NotificationDelivery))
        assert result.attempt_count == 3
        assert result.error_code == "transport_error"
        assert "credential-do-not-store" not in (result.error_summary or "")
        assert row is not None
        assert "credential-do-not-store" not in (row.error_summary or "")


def test_unexpected_adapter_error_isolated_and_redacted() -> None:
    with setup_session() as session:
        channel = add_channel(session, "WeChat backup", "pushplus")
        notifier = StubNotifier(
            "pushplus", [RuntimeError("credential-do-not-store")]
        )

        result = NotificationDeliveryService(session).deliver(
            channel, notifier, Notification("Digest", "Body")
        )

        row = session.scalar(select(NotificationDelivery))
        assert result.status is DeliveryStatus.FAILED
        assert result.retryable is False
        assert result.error_code == "delivery_failed"
        assert result.attempt_count == 1
        assert row is not None
        assert row.status == "failed"
        assert "credential-do-not-store" not in (row.error_summary or "")


def test_success_on_third_attempt_persists_final_timestamp() -> None:
    with setup_session() as session:
        channel = add_channel(session, "Primary email", "email")
        notifier = StubNotifier(
            "email",
            [
                failed("email", retryable=True),
                failed("email", retryable=True),
                DeliveryResult.success(provider="email"),
            ],
        )

        result = NotificationDeliveryService(
            session, sleep=lambda _seconds: None, clock=lambda: NOW
        ).deliver(channel, notifier, Notification("Digest", "Body"))

        row = session.scalar(select(NotificationDelivery))
        assert result.status is DeliveryStatus.SUCCESS
        assert result.attempt_count == 3
        assert row is not None
        assert row.status == "success"
        assert row.attempt_count == 3
        assert row.error_summary is None
        assert row.delivered_at == NOW


def test_email_success_remains_overall_success_when_wechat_fails() -> None:
    with setup_session() as session:
        email_channel = add_channel(session, "Primary email", "email")
        wechat_channel = add_channel(session, "WeChat backup", "pushplus")
        email = StubNotifier("email", [DeliveryResult.success(provider="email")])
        wechat = StubNotifier("pushplus", [failed("pushplus", retryable=False)])

        report = NotificationDeliveryService(session).deliver_channels(
            Notification("Digest", "Body"),
            {
                "email": (email_channel, email),
                "wechat": (wechat_channel, wechat),
            },
            primary_channel="email",
        )

        rows = session.scalars(
            select(NotificationDelivery).order_by(NotificationDelivery.channel_id)
        ).all()
        assert report.status is DeliveryStatus.SUCCESS
        assert report.results["email"].status is DeliveryStatus.SUCCESS
        assert report.results["wechat"].status is DeliveryStatus.FAILED
        assert [(row.channel_id, row.status) for row in rows] == [
            (email_channel.id, "success"),
            (wechat_channel.id, "failed"),
        ]


def test_notification_value_object_rejects_empty_or_oversized_fields() -> None:
    for title, body in (
        ("", "Body"),
        ("Digest", ""),
        ("x" * 257, "Body"),
        ("Digest\r\nBcc: victim@example.com", "Body"),
    ):
        try:
            Notification(title, body)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid notification must be rejected")
