from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/finance.db"
    secret_key: str = "development-only-change-me"
    timezone: str = "Asia/Shanghai"
    session_https_only: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="FINANCE_")


@lru_cache
def get_settings() -> Settings:
    return Settings()
