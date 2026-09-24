from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from finance_app.config import Settings
from finance_app.notifications.base import DeliveryResult, Notification


class EmailNotifier:
    provider = "email"

    def __init__(self, settings: Settings) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._security = settings.smtp_security
        self._username = settings.smtp_username
        self._authorization_code = settings.smtp_authorization_code
        self._sender = settings.smtp_sender
        self._recipient = settings.smtp_recipient

    def send(self, notification: Notification) -> DeliveryResult:
        if not self._is_configured():
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="invalid_configuration",
                error_summary="invalid_configuration: email channel is incomplete",
                retryable=False,
            )

        assert self._host is not None
        assert self._port is not None
        assert self._username is not None
        assert self._authorization_code is not None
        assert self._sender is not None
        assert self._recipient is not None

        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = self._recipient
        message["Subject"] = notification.title
        message.set_content(notification.body)
        context = ssl.create_default_context()

        try:
            if self._security == "ssl":
                with smtplib.SMTP_SSL(
                    self._host, self._port, timeout=10.0, context=context
                ) as client:
                    self._authenticate_and_send(client, message)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=10.0) as client:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                    self._authenticate_and_send(client, message)
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="authentication_failed",
                error_summary="authentication_failed: SMTP rejected credentials",
                retryable=False,
            )
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="recipient_rejected",
                error_summary="recipient_rejected: SMTP rejected the envelope",
                retryable=False,
            )
        except smtplib.SMTPResponseException as error:
            retryable = 400 <= error.smtp_code < 500
            return DeliveryResult.failed(
                provider=self.provider,
                error_code=("temporary_failure" if retryable else "delivery_failed"),
                error_summary=(
                    "temporary_failure: SMTP delivery is temporarily unavailable"
                    if retryable
                    else "delivery_failed: SMTP permanently rejected delivery"
                ),
                retryable=retryable,
            )
        except smtplib.SMTPNotSupportedError:
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="invalid_configuration",
                error_summary="invalid_configuration: SMTP security is unsupported",
                retryable=False,
            )
        except (OSError, smtplib.SMTPException):
            return DeliveryResult.failed(
                provider=self.provider,
                error_code="transport_error",
                error_summary="transport_error: SMTP delivery failed",
                retryable=True,
            )

        return DeliveryResult.success(provider=self.provider)

    def _authenticate_and_send(
        self, client: smtplib.SMTP, message: EmailMessage
    ) -> None:
        assert self._username is not None
        assert self._authorization_code is not None
        client.login(self._username, self._authorization_code.get_secret_value())
        client.send_message(message)

    def _is_configured(self) -> bool:
        return all(
            value is not None
            for value in (
                self._host,
                self._port,
                self._username,
                self._authorization_code,
                self._sender,
                self._recipient,
            )
        )
