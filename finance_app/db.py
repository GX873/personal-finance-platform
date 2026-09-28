from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from finance_app.config import get_settings


def utc_now() -> datetime:
    """Return an aware timestamp so application-created records are unambiguous."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class UtcDateTime(TypeDecorator[datetime]):
    """Store UTC-naive values for SQLite but always return aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def configure_sqlite(engine: Engine) -> Engine:
    """Apply connection-level SQLite safety and concurrency settings."""

    if engine.dialect.name != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def ensure_sqlite_database_parent(database_url: str) -> None:
    """Create a file-backed SQLite database's parent before opening it."""
    if not database_url.startswith("sqlite"):
        return
    database_path = make_url(database_url).database
    if (
        database_path
        and database_path != ":memory:"
        and not database_path.startswith("file:")
    ):
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    options: dict[str, object] = {}
    if url.startswith("sqlite"):
        ensure_sqlite_database_parent(url)
        options["connect_args"] = {"check_same_thread": False}
    return configure_sqlite(create_engine(url, **options))


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(
        bind=create_db_engine(database_url), autoflush=False, expire_on_commit=False
    )


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    global _session_factory
    if engine is not None:
        return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


def reset_database_state() -> None:
    """Dispose cached state for isolated tests and controlled process shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
