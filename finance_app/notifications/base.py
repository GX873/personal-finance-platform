from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z", re.ASCII)
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)


class DeliveryStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class Notification:
    title: str
    body: str

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("notification title is required")
        if len(self.title) > 256:
            raise ValueError("notification title must not exceed 256 characters")
        if "\r" in self.title or "\n" in self.title:
            raise ValueError("notification title must be a single line")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("notification body is required")


@dataclass(frozen=True)
class DeliveryResult:
    provider: str
    status: DeliveryStatus
    retryable: bool = False
    error_code: str | None = None
    error_summary: str | None = None
    attempt_count: int = 1
    http_status: int | None = None

    def __post_init__(self) -> None:
        if _PROVIDER.fullmatch(self.provider) is None:
            raise ValueError("delivery provider is invalid")
        if type(self.attempt_count) is not int or self.attempt_count < 1:
            raise ValueError("attempt_count must be a positive integer")
        if self.status is DeliveryStatus.SUCCESS:
            if (
                self.retryable
                or self.error_code is not None
                or self.error_summary is not None
                or self.http_status is not None
            ):
                raise ValueError("successful delivery cannot contain an error")
        elif (
            self.error_code is None
            or _ERROR_CODE.fullmatch(self.error_code) is None
            or self.error_summary is None
            or not self.error_summary
            or len(self.error_summary) > 512
        ):
            raise ValueError("failed delivery requires a safe error code and summary")
        if self.http_status is not None and not 100 <= self.http_status <= 599:
            raise ValueError("http_status must be a valid HTTP status")

    @classmethod
    def success(cls, *, provider: str) -> DeliveryResult:
        return cls(provider=provider, status=DeliveryStatus.SUCCESS)

    @classmethod
    def failed(
        cls,
        *,
        provider: str,
        error_code: str,
        error_summary: str,
        retryable: bool,
        http_status: int | None = None,
    ) -> DeliveryResult:
        return cls(
            provider=provider,
            status=DeliveryStatus.FAILED,
            retryable=retryable,
            error_code=error_code,
            error_summary=error_summary,
            http_status=http_status,
        )

    def with_attempt_count(self, attempt_count: int) -> DeliveryResult:
        return replace(self, attempt_count=attempt_count)


class NotificationAdapter(Protocol):
    provider: str

    def send(self, notification: Notification) -> DeliveryResult: ...
