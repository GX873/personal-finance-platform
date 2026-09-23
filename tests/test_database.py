from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from finance_app.auth.models import User
from finance_app.db import Base, configure_sqlite, create_db_engine
from finance_app.ledger.models import Account, Asset, Holding, Transaction
from finance_app.notifications.models import AppSetting, ImportBatch, JobRun
from finance_app.portfolio.models import PriceSnapshot


@pytest.fixture
def db_session(tmp_path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'finance.db'}")
    configure_sqlite(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_sqlite_enables_foreign_keys_wal_and_busy_timeout(db_session: Session):
    assert db_session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    assert db_session.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
    assert db_session.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000


def test_engine_creates_configured_sqlite_parent_directory(tmp_path):
    database_path = tmp_path / "runtime-data" / "finance.db"
    engine = create_db_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    assert database_path.exists()


def test_alembic_upgrade_creates_configured_sqlite_parent_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    database_path = tmp_path / "migration-data" / "finance.db"
    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{database_path}")
    from finance_app.config import get_settings

    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
    finally:
        get_settings.cache_clear()
    assert database_path.exists()


def test_schema_uses_integer_cents_decimal_values_and_utc_timestamps(
    db_session: Session,
):
    account = Account(name="cash", kind="cash", currency="CNY")
    asset = Asset(code="000001", market="CN", name="Fund")
    db_session.add_all((account, asset))
    db_session.commit()

    holding = Holding(
        account_id=account.id, asset_id=asset.id, quantity=Decimal("1.25")
    )
    transaction = Transaction(
        source="manual",
        external_id="row-1",
        kind="buy",
        account_id=account.id,
        asset_id=asset.id,
        amount_cents=10_000,
        quantity=Decimal("1.25"),
        price=Decimal(8000),
    )
    db_session.add_all((holding, transaction))
    db_session.commit()

    assert isinstance(account.opening_balance_cents, int)
    assert holding.quantity == Decimal("1.25000000")
    assert transaction.amount_cents == 10_000
    assert transaction.created_at.tzinfo is not None


def test_schema_enforces_required_unique_keys(db_session: Session):
    account = Account(name="cash", kind="cash", currency="CNY")
    asset = Asset(code="000001", market="CN", name="Fund")
    db_session.add_all((User(username="admin", password_hash="hash"), account, asset))
    db_session.commit()
    db_session.add(User(username="admin", password_hash="other"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add_all(
        (
            Transaction(
                source="import",
                external_id="1",
                kind="buy",
                account_id=account.id,
                asset_id=asset.id,
            ),
            ImportBatch(sha256="abc"),
            AppSetting(key="timezone", value="Asia/Shanghai"),
            JobRun(job_name="daily-check", business_date=date(2026, 9, 23)),
        )
    )
    db_session.commit()
    db_session.add_all(
        (
            Transaction(
                source="import",
                external_id="1",
                kind="buy",
                account_id=account.id,
                asset_id=asset.id,
            ),
            ImportBatch(sha256="abc"),
            AppSetting(key="timezone", value="UTC"),
            JobRun(job_name="daily-check", business_date=date(2026, 9, 23)),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_metadata_includes_initial_migration_tables_and_constraints(
    db_session: Session,
):
    inspector = inspect(db_session.bind)
    assert set(inspector.get_table_names()) == {
        "accounts",
        "alerts",
        "allocation_targets",
        "app_settings",
        "assets",
        "audit_events",
        "cash_buckets",
        "holdings",
        "import_batches",
        "job_runs",
        "monthly_budgets",
        "notification_channels",
        "notification_deliveries",
        "portfolio_snapshots",
        "price_snapshots",
        "transactions",
        "users",
    }
    constraints = {
        item["name"] for item in inspector.get_unique_constraints("price_snapshots")
    }
    assert "uq_price_snapshots_asset_valuation_source" in constraints
    assert PriceSnapshot.__table__.c.price.type.precision == 24
    assert PriceSnapshot.__table__.c.price.type.scale == 8
