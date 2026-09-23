from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from finance_app.config import get_settings
    from finance_app.db import reset_database_state

    reset_database_state()
    get_settings.cache_clear()
    try:
        yield
    finally:
        reset_database_state()
        get_settings.cache_clear()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{tmp_path / 'finance.db'}")

    from finance_app.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
