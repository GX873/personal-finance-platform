from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Column, Integer, Table, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from finance_app.auth.models import User
from finance_app.db import (
    Base,
    create_db_engine,
    get_engine,
    get_session_factory,
    reset_database_state,
    utc_now,
)
from finance_app.ledger.models import (
    Account,
    Asset,
    Holding,
    MonthlyBudget,
    PortfolioRole,
    RiskLevel,
    Transaction,
)
from finance_app.notifications.models import AppSetting, ImportBatch, JobRun
from finance_app.portfolio.models import PriceSnapshot


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_initial_revision_is_independent_of_future_models(
    tmp_path, monkeypatch: pytest.MonkeyPatch, direction: str
):
    database_url = f"sqlite:///{tmp_path / 'frozen-migration.db'}"
    monkeypatch.setenv("FINANCE_DATABASE_URL", database_url)
    config = Config("alembic.ini")
    engine = create_db_engine(database_url)
    future_table = None
    try:
        if direction == "downgrade":
            command.upgrade(config, "head")
        future_table = Table(
            "future_unrelated_table",
            Base.metadata,
            Column("id", Integer, primary_key=True),
        )
        if direction == "upgrade":
            command.upgrade(config, "head")
            assert future_table.name not in inspect(engine).get_table_names()
        else:
            future_table.create(engine)
            with engine.begin() as connection:
                connection.execute(future_table.insert().values(id=42))
            command.downgrade(config, "base")
            assert set(inspect(engine).get_table_names()) == {
                "alembic_version",
                future_table.name,
            }
            with engine.connect() as connection:
                assert connection.execute(future_table.select()).scalar_one() == 42
    finally:
        if future_table is not None:
            Base.metadata.remove(future_table)
        engine.dispose()


def test_initial_revision_matches_current_model_schema(db_session: Session):
    command.check(Config("alembic.ini"))


@pytest.mark.parametrize(
    "month", ["2026-00", "2026-13", "2026-19", "2026-1", "202x-12"]
)
def test_monthly_budget_rejects_invalid_months(db_session: Session, month: str):
    db_session.add(MonthlyBudget(month=month, bucket_kind="cash", amount_cents=100))
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize("month", ["2026-01", "2026-12"])
def test_monthly_budget_accepts_month_boundaries(db_session: Session, month: str):
    db_session.add(MonthlyBudget(month=month, bucket_kind="cash", amount_cents=100))
    db_session.commit()


@pytest.fixture
def db_session(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    database_url = f"sqlite:///{tmp_path / 'finance.db'}"
    monkeypatch.setenv("FINANCE_DATABASE_URL", database_url)
    from finance_app.config import get_settings

    get_settings.cache_clear()
    reset_database_state()
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_db_engine(database_url)
    session = get_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        reset_database_state()


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


def test_application_session_factory_reuses_one_engine(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    from finance_app.config import get_settings

    get_settings.cache_clear()
    reset_database_state()
    try:
        first_engine = get_engine()
        second_engine = get_engine()
        assert first_engine is second_engine
        assert get_session_factory().kw["bind"] is first_engine
    finally:
        reset_database_state()


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


def test_persisted_timestamps_are_utc_aware_after_reload(db_session: Session):
    account = Account(name="cash", kind="cash", currency="CNY")
    db_session.add(account)
    db_session.commit()
    account_id = account.id
    db_session.expunge_all()

    reloaded = db_session.get(Account, account_id)
    assert reloaded is not None
    assert reloaded.created_at.tzinfo is not None
    assert reloaded.created_at.utcoffset().total_seconds() == 0
    assert reloaded.created_at <= utc_now()


def test_assets_and_holdings_capture_risk_role_and_price_freshness(db_session: Session):
    account = Account(name="broker", kind="brokerage", currency="CNY")
    asset = Asset(
        code="000001",
        market="CN",
        name="Fund",
        risk_level=RiskLevel.MEDIUM,
        portfolio_role=PortfolioRole.CORE,
    )
    db_session.add_all((account, asset))
    db_session.commit()

    holding = Holding(
        account_id=account.id,
        asset_id=asset.id,
        quantity=Decimal(1),
        valuation_cents=123_45,
        last_price=Decimal("123.45"),
        price_source="manual",
        is_price_stale=False,
    )
    db_session.add(holding)
    db_session.commit()
    assert holding.valuation_cents == 123_45
    assert holding.last_price == Decimal("123.45000000")


def test_schema_enforces_required_unique_keys(db_session: Session):
    account = Account(name="cash", kind="cash", currency="CNY")
    asset = Asset(code="000001", market="CN", name="Fund")
    db_session.add_all((User(username="admin", password_hash="hash"), account, asset))
    db_session.commit()
    db_session.add(User(username="admin", password_hash="other"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_schema_rejects_negative_fees(db_session: Session):
    account = Account(name="cash", kind="cash", currency="CNY")
    asset = Asset(code="000001", market="CN", name="Fund")
    db_session.add_all((account, asset))
    db_session.commit()
    db_session.add(
        Transaction(
            source="manual",
            external_id="negative-fee",
            kind="buy",
            account_id=account.id,
            asset_id=asset.id,
            fee_cents=-1,
        )
    )
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
    assert set(inspector.get_table_names()) - {"alembic_version"} == {
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
    assert (
        db_session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        == "0001"
    )
