from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{tmp_path / 'finance.db'}")

    from finance_app.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
