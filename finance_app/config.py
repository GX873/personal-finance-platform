import re
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\Z"
)
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\Z"
)
_SERVERCHAN_SENDKEY = re.compile(r"[A-Za-z0-9_-]{1,256}\Z", re.ASCII)


def validate_serverchan_sendkey_value(value: str) -> None:
    if _SERVERCHAN_SENDKEY.fullmatch(value) is None:
        raise ValueError("serverchan_sendkey must be an opaque token")


def validate_opaque_secret_value(value: str) -> None:
    if not value or len(value) > 512 or any(ord(char) < 33 for char in value):
        raise ValueError("notification credential is invalid")


def validate_wecom_webhook_url_value(value: str) -> None:
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "qyapi.weixin.qq.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/cgi-bin/webhook/send"
        or parsed.fragment
        or set(query) != {"key"}
        or len(query["key"]) != 1
    ):
        raise ValueError("wecom_webhook_url must be an official HTTPS webhook")
    try:
        validate_serverchan_sendkey_value(query["key"][0])
    except ValueError:
        raise ValueError(
            "wecom_webhook_url must be an official HTTPS webhook"
        ) from None


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/finance.db"
    secret_key: str = "development-only-change-me"
    timezone: str = "Asia/Shanghai"
    session_https_only: bool = False
    environment: Literal["development", "production"] = "development"
    demo_mode: bool = False
    import_upload_dir: str = "./data/imports"
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_security: Literal["starttls", "ssl"] = "starttls"
    smtp_username: str | None = None
    smtp_authorization_code: SecretStr | None = Field(
        default=None, repr=False, exclude=True
    )
    smtp_sender: str | None = None
    smtp_recipient: str | None = None
    pushplus_token: SecretStr | None = Field(default=None, repr=False, exclude=True)
    serverchan_sendkey: SecretStr | None = Field(
        default=None, repr=False, exclude=True
    )
    wecom_webhook_url: SecretStr | None = Field(
        default=None, repr=False, exclude=True
    )

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="FINANCE_", hide_input_in_errors=True
    )

    @field_validator("smtp_host")
    @classmethod
    def validate_smtp_host(cls, value: str | None) -> str | None:
        if value is not None and _HOSTNAME.fullmatch(value) is None:
            raise ValueError("smtp_host must be a hostname without a scheme or path")
        return value

    @field_validator("smtp_port")
    @classmethod
    def validate_smtp_port(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        return value

    @field_validator("smtp_sender", "smtp_recipient")
    @classmethod
    def validate_email_address(cls, value: str | None) -> str | None:
        if value is not None and _EMAIL.fullmatch(value) is None:
            raise ValueError("notification email address is invalid")
        return value

    @field_validator("smtp_authorization_code", "pushplus_token")
    @classmethod
    def validate_opaque_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            validate_opaque_secret_value(value.get_secret_value())
        return value

    @field_validator("serverchan_sendkey")
    @classmethod
    def validate_serverchan_sendkey(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        if value is not None:
            validate_serverchan_sendkey_value(value.get_secret_value())
        return value

    @field_validator("wecom_webhook_url")
    @classmethod
    def validate_wecom_webhook_url(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        validate_wecom_webhook_url_value(raw)
        return value

    @model_validator(mode="after")
    def validate_production_secret(self) -> "Settings":
        if self.environment == "production" and self.demo_mode:
            raise ValueError("Production does not allow demo_mode")
        if self.environment == "production" and (
            self.secret_key == "development-only-change-me" or len(self.secret_key) < 32
        ):
            raise ValueError(
                "Production requires a unique secret_key of at least 32 characters"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
