from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import ClassVar, Self

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from finance_app.config import Settings
from finance_app.db import Base, create_db_engine
from finance_app.notifications.base import DeliveryStatus, Notification
from finance_app.notifications.email import EmailNotifier
from finance_app.notifications.models import NotificationChannel
from finance_app.notifications.service import NotificationDeliveryService


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []
    login_error: ClassVar[Exception | None] = None

    def __init__(
        self, host: str, port: int, *, timeout: float, context: object | None = None
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ssl_context = context
        self.ehlo_count = 0
        self.starttls_context: object | None = None
        self.login_args: tuple[str, str] | None = None
        self.message: EmailMessage | None = None
        type(self).instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def ehlo(self) -> None:
        self.ehlo_count += 1

    def starttls(self, *, context: object) -> None:
        self.starttls_context = context

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)
        if self.login_error is not None:
            raise self.login_error

    def send_message(self, message: EmailMessage) -> None:
        self.message = message


@pytest.fixture(autouse=True)
def reset_fake_smtp() -> None:
    FakeSMTP.instances.clear()
    FakeSMTP.login_error = None


@pytest.fixture
def smtp_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    values = {
        "FINANCE_SMTP_HOST": "smtp.example.com",
        "FINANCE_SMTP_PORT": "587",
        "FINANCE_SMTP_SECURITY": "starttls",
        "FINANCE_SMTP_USERNAME": "finance@example.com",
        "FINANCE_SMTP_AUTHORIZATION_CODE": "smtp-secret-code",
        "FINANCE_SMTP_SENDER": "finance@example.com",
        "FINANCE_SMTP_RECIPIENT": "owner@example.com",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_email_uses_starttls_and_authorization_code(
    monkeypatch: pytest.MonkeyPatch, smtp_settings: Settings
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    result = EmailNotifier(smtp_settings).send(
        Notification(title="Daily finance digest", body="No action required")
    )

    smtp = FakeSMTP.instances[0]
    assert result.status is DeliveryStatus.SUCCESS
    assert result.provider == "email"
    assert result.error_summary is None
    assert smtp.host == "smtp.example.com"
    assert smtp.port == 587
    assert smtp.timeout == 10.0
    assert smtp.ehlo_count == 2
    assert smtp.starttls_context is not None
    assert smtp.login_args == ("finance@example.com", "smtp-secret-code")
    assert smtp.message is not None
    assert smtp.message["From"] == "finance@example.com"
    assert smtp.message["To"] == "owner@example.com"
    assert smtp.message["Subject"] == "Daily finance digest"


def test_email_ssl_uses_smtp_ssl_without_starttls(
    monkeypatch: pytest.MonkeyPatch, smtp_settings: Settings
) -> None:
    monkeypatch.setenv("FINANCE_SMTP_SECURITY", "ssl")
    ssl_settings = Settings()
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(
        smtplib,
        "SMTP",
        lambda *_args, **_kwargs: pytest.fail("STARTTLS client must not be used"),
    )

    result = EmailNotifier(ssl_settings).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.SUCCESS
    assert FakeSMTP.instances[0].starttls_context is None
    assert FakeSMTP.instances[0].ssl_context is not None
    assert FakeSMTP.instances[0].login_args == (
        "finance@example.com",
        "smtp-secret-code",
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FINANCE_SMTP_HOST", "https://attacker.example/smtp"),
        ("FINANCE_SMTP_HOST", "smtp.example.com\r\nX-Injected: yes"),
        ("FINANCE_SMTP_PORT", "0"),
        ("FINANCE_SMTP_PORT", "65536"),
        ("FINANCE_SMTP_SENDER", "not-an-email"),
        ("FINANCE_SMTP_RECIPIENT", "owner@example.com,other@example.com"),
    ],
)
def test_smtp_configuration_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    smtp_settings: Settings,
    name: str,
    value: str,
) -> None:
    del smtp_settings
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_authorization_code_is_hidden_from_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "smtp-credential secret"
    monkeypatch.setenv("FINANCE_SMTP_AUTHORIZATION_CODE", secret)

    with pytest.raises(ValidationError) as caught:
        Settings()

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_missing_email_configuration_is_a_permanent_safe_failure() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    result = EmailNotifier(settings).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is False
    assert result.error_code == "invalid_configuration"
    assert result.error_summary == "invalid_configuration: email channel is incomplete"


def test_authentication_error_is_permanent_and_never_exposes_authorization_code(
    monkeypatch: pytest.MonkeyPatch,
    smtp_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(
        535, b"smtp-secret-code rejected"
    )

    result = EmailNotifier(smtp_settings).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is False
    assert result.error_code == "authentication_failed"
    exposed = f"{result!r}\n{smtp_settings!r}\n{caplog.text}"
    assert "smtp-secret-code" not in exposed


def test_transient_smtp_error_is_retryable_without_echoing_server_text(
    monkeypatch: pytest.MonkeyPatch, smtp_settings: Settings
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.login_error = smtplib.SMTPServerDisconnected(
        "smtp-secret-code disconnected"
    )

    result = EmailNotifier(smtp_settings).send(Notification("Digest", "Body"))

    assert result.status is DeliveryStatus.FAILED
    assert result.retryable is True
    assert result.error_code == "transport_error"
    assert "smtp-secret-code" not in (result.error_summary or "")


@pytest.mark.parametrize(
    ("status", "error_code", "retryable"),
    [
        (451, "temporary_failure", True),
        (550, "delivery_failed", False),
    ],
)
def test_smtp_response_codes_have_enforced_retry_classification(
    monkeypatch: pytest.MonkeyPatch,
    smtp_settings: Settings,
    status: int,
    error_code: str,
    retryable: bool,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.login_error = smtplib.SMTPDataError(
        status, b"smtp-secret-code provider detail"
    )

    result = EmailNotifier(smtp_settings).send(Notification("Digest", "Body"))

    assert result.error_code == error_code
    assert result.retryable is retryable
    assert "smtp-secret-code" not in (result.error_summary or "")


@pytest.mark.parametrize(
    ("refusals", "error_code", "retryable"),
    [
        (
            {"temporary@example.com": (450, b"temporary secret detail")},
            "temporary_failure",
            True,
        ),
        (
            {"permanent@example.com": (550, b"permanent secret detail")},
            "recipient_rejected",
            False,
        ),
        (
            {
                "temporary@example.com": (451, b"temporary secret detail"),
                "permanent@example.com": (550, b"permanent secret detail"),
            },
            "temporary_failure",
            True,
        ),
        (
            {
                "accepted@example.com": (250, b"accepted secret detail"),
                "temporary@example.com": (451, b"temporary secret detail"),
            },
            "recipient_rejected",
            False,
        ),
    ],
)
def test_recipient_refusals_use_safe_aggregate_smtp_classification(
    monkeypatch: pytest.MonkeyPatch,
    smtp_settings: Settings,
    refusals: dict[str, tuple[int, bytes]],
    error_code: str,
    retryable: bool,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.login_error = smtplib.SMTPRecipientsRefused(refusals)

    result = EmailNotifier(smtp_settings).send(Notification("Digest", "Body"))

    assert result.error_code == error_code
    assert result.retryable is retryable
    rendered = result.error_summary or ""
    assert "example.com" not in rendered
    assert "secret detail" not in rendered
    assert "smtp-secret-code" not in rendered


@pytest.mark.parametrize(
    ("status", "error_code", "retryable"),
    [
        (451, "temporary_failure", True),
        (550, "recipient_rejected", False),
    ],
)
def test_sender_refusal_uses_smtp_status_without_exposing_details(
    monkeypatch: pytest.MonkeyPatch,
    smtp_settings: Settings,
    status: int,
    error_code: str,
    retryable: bool,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.login_error = smtplib.SMTPSenderRefused(
        status, b"sender secret detail", "sender-secret@example.com"
    )

    result = EmailNotifier(smtp_settings).send(Notification("Digest", "Body"))

    assert result.error_code == error_code
    assert result.retryable is retryable
    rendered = result.error_summary or ""
    assert "sender-secret" not in rendered
    assert "secret detail" not in rendered
    assert "smtp-secret-code" not in rendered


def test_temporary_recipient_refusal_retries_three_times_with_bounded_delays(
    monkeypatch: pytest.MonkeyPatch, smtp_settings: Settings
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.login_error = smtplib.SMTPRecipientsRefused(
        {"private@example.com": (450, b"smtp-secret-code provider detail")}
    )
    engine = create_db_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sleeps: list[float] = []

    with Session(engine) as session:
        channel = NotificationChannel(name="Primary email", channel_type="email")
        session.add(channel)
        session.flush()
        result = NotificationDeliveryService(
            session, sleep=sleeps.append
        ).deliver(
            channel,
            EmailNotifier(smtp_settings),
            Notification("Digest", "Body"),
        )

    assert result.status is DeliveryStatus.FAILED
    assert result.attempt_count == 3
    assert result.retryable is True
    assert len(FakeSMTP.instances) == 3
    assert sleeps == [1.0, 3.0]
    assert "smtp-secret-code" not in (result.error_summary or "")
