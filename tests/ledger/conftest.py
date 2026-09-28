from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.orm import Session

from alembic import command
from finance_app.db import create_db_engine


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    monkeypatch.setenv("FINANCE_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(url)
    try:
        with Session(engine) as value:
            yield value
    finally:
        engine.dispose()
