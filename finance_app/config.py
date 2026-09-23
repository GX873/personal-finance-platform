from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/finance.db"
    secret_key: str = "development-only-change-me"
    timezone: str = "Asia/Shanghai"
    session_https_only: bool = False
    environment: Literal["development", "production"] = "development"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="FINANCE_")

    @model_validator(mode="after")
    def validate_production_secret(self) -> "Settings":
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
