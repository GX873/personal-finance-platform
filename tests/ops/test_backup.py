from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from finance_app.db import create_db_engine, get_session_factory
from finance_app.ledger.models import AuditEvent
from finance_app.ops import backup as backup_module
from finance_app.ops.backup import create_backup, restore_check, verify_backup


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "finance.db"
    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{path}")
    command.upgrade(Config("alembic.ini"), "head")
    return path


def fixed_clock() -> datetime:
    return datetime(2026, 9, 24, 8, 0, tzinfo=UTC)


def test_backup_passes_integrity_and_application_schema(populated_db: Path, tmp_path: Path):
    backup = create_backup(populated_db, tmp_path / "backups")
    result = verify_backup(backup, require_checksum=True)
    assert result.integrity_check == "ok"
    assert result.schema_valid is True
    assert result.schema_revision == "0002"


def test_restore_check_rejects_non_application_schema(tmp_path: Path):
    candidate = tmp_path / "finance-20260924-080000.sqlite3"
    with sqlite3.connect(candidate) as db:
        db.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
    candidate.with_name(candidate.name + ".sha256").write_text(
        hashlib.sha256(candidate.read_bytes()).hexdigest(), encoding="ascii"
    )
    with pytest.raises(ValueError, match="schema"):
        restore_check(candidate)


def test_retention_ignores_corrupt_files(populated_db: Path, tmp_path: Path):
    directory = tmp_path / "backups"
    good = [create_backup(populated_db, directory, keep=20, clock=fixed_clock) for _ in range(3)]
    invalid = directory / "finance-20990101-000000.sqlite3"
    invalid.write_bytes(b"not sqlite")
    invalid.with_name(invalid.name + ".sha256").write_text(
        hashlib.sha256(invalid.read_bytes()).hexdigest(), encoding="ascii"
    )
    os.utime(invalid, (2_000_000_000, 2_000_000_000))
    backup_module._retain_verified(directory, keep=2)
    assert invalid.exists()
    assert len([path for path in good if path.exists()]) == 2


def test_retention_uses_mtime_for_double_digit_collision_suffixes(populated_db: Path, tmp_path: Path):
    directory = tmp_path / "backups"
    backups = [create_backup(populated_db, directory, keep=20, clock=fixed_clock) for _ in range(11)]
    for index, path in enumerate(backups):
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    backup_module._retain_verified(directory, keep=2)
    assert {path.name for path in directory.glob("*.sqlite3")} == {
        backups[-2].name,
        backups[-1].name,
    }


def test_keep_is_checked_before_any_write(populated_db: Path, tmp_path: Path):
    directory = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="keep"):
        create_backup(populated_db, directory, keep=0)
    assert not directory.exists()


def test_audit_failure_removes_new_files_and_preserves_old_backup(
    populated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "backups"
    existing = create_backup(populated_db, directory)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(backup_module, "_record_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit"):
        create_backup(populated_db, directory, session=object(), clock=fixed_clock)  # type: ignore[arg-type]
    assert list(directory.glob("*.sqlite3")) == [existing]
    assert existing.with_name(existing.name + ".sha256").exists()


def test_sidecar_publish_failure_removes_new_database_and_preserves_old_backup(
    populated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "backups"
    existing = create_backup(populated_db, directory)
    original_replace = Path.replace

    def fail_sidecar_replace(path: Path, target: Path):
        if str(path).endswith(".sha256.tmp"):
            raise OSError("sidecar publish failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_sidecar_replace)
    with pytest.raises(OSError, match="sidecar"):
        create_backup(populated_db, directory, clock=fixed_clock)
    assert list(directory.glob("*.sqlite3")) == [existing]
    assert existing.with_name(existing.name + ".sha256").exists()


def test_environment_secret_is_not_copied_into_backup(
    populated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "environment-only-super-secret"
    monkeypatch.setenv("FINANCE_PUSHPLUS_TOKEN", secret)
    backup = create_backup(populated_db, tmp_path / "backups")
    assert secret.encode() not in backup.read_bytes()


def test_backup_and_restore_check_are_audited(populated_db: Path, tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{populated_db}")
    session = get_session_factory(engine)()
    try:
        backup = create_backup(populated_db, tmp_path / "backups", session=session)
        restore_check(backup, session=session)
        assert list(session.scalars(select(AuditEvent.event_type).order_by(AuditEvent.id))) == [
            "backup.created",
            "backup.restore_checked",
        ]
    finally:
        session.close()
        engine.dispose()


def test_restore_check_rejects_bad_checksum(populated_db: Path, tmp_path: Path):
    backup = create_backup(populated_db, tmp_path / "backups")
    backup.with_name(backup.name + ".sha256").write_text("0" * 64, encoding="ascii")
    with pytest.raises(ValueError, match="checksum"):
        restore_check(backup)
