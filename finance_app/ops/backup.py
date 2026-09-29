from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from finance_app.config import get_settings
from finance_app.db import utc_now
from finance_app.ledger.models import AuditEvent


@dataclass(frozen=True)
class BackupVerification:
    path: Path
    checksum: str
    integrity_check: str
    schema_valid: bool
    schema_revision: str | None


@dataclass(frozen=True)
class BackupStatus:
    path: Path
    modified_at: datetime
    size_bytes: int
    integrity_check: str
    schema_valid: bool


def backup_directory(source: Any | None = None) -> Path:
    return _database_path(source).parent / "backups"


def recent_backup_statuses(
    directory: str | Path, *, limit: int = 14
) -> list[BackupStatus]:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        return []
    statuses: list[BackupStatus] = []
    paths = sorted(
        root.glob("finance-*.sqlite3"),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
        reverse=True,
    )[:limit]
    for path in paths:
        stat = path.stat()
        try:
            result = verify_backup(path, require_checksum=True)
            integrity = result.integrity_check
            schema_valid = result.schema_valid
        except (OSError, ValueError):
            integrity = "invalid"
            schema_valid = False
        statuses.append(
            BackupStatus(
                path=path,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                size_bytes=stat.st_size,
                integrity_check=integrity,
                schema_valid=schema_valid,
            )
        )
    return statuses


_REQUIRED_SCHEMA = {
    "accounts": {"id", "name", "kind", "opening_balance_cents"},
    "assets": {"id", "code", "market", "name"},
    "holdings": {"id", "account_id", "asset_id", "quantity", "cost_cents"},
    "transactions": {"id", "source", "external_id", "kind", "account_id"},
    "audit_events": {"id", "event_type", "created_at"},
    "alembic_version": {"version_num"},
}
_SCHEMA_REVISION = "0004"


def _database_path(source: Any | None) -> Path:
    """Resolve a SQLite source without ever copying the live file directly."""
    if isinstance(source, (str, Path)):
        value = str(source)
        if value.startswith("sqlite"):
            url = make_url(value)
            if url.database in (None, ":memory:") or str(url.database).startswith("file:"):
                raise ValueError("a file-backed SQLite database is required")
            return Path(str(url.database)).resolve()
        return Path(source).expanduser().resolve()
    if isinstance(source, Session):
        return _database_path(source.get_bind())
    if source is not None and (isinstance(source, Engine) or hasattr(source, "url")):
        url = make_url(str(source.url))
        if url.get_backend_name() != "sqlite" or url.database in (None, ":memory:"):
            raise ValueError("a file-backed SQLite database is required")
        return Path(str(url.database)).resolve()
    if source is not None and hasattr(source, "engine"):
        return _database_path(source.engine)
    if source is None:
        return _database_path(get_settings().database_url)
    raise TypeError("source must be a SQLite path, URL, Engine, or Session")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _remove_sqlite_auxiliary_files(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _read_expected_checksum(path: Path) -> str | None:
    sidecar = _sidecar(path)
    if not sidecar.exists():
        return None
    first = sidecar.read_text(encoding="ascii").strip().split()
    if not first or len(first[0]) != 64 or any(c not in "0123456789abcdefABCDEF" for c in first[0]):
        raise ValueError("invalid checksum sidecar")
    return first[0].lower()


def verify_backup(path: str | Path, *, require_checksum: bool = False) -> BackupVerification:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise ValueError("backup path must be an existing SQLite file")
    checksum = _sha256(candidate)
    expected = _read_expected_checksum(candidate)
    if expected is not None and checksum != expected:
        raise ValueError("backup checksum does not match sidecar")
    if require_checksum and expected is None:
        raise ValueError("backup checksum sidecar is missing")
    db = None
    try:
        db = sqlite3.connect(
            f"file:{candidate.as_posix()}?mode=ro&immutable=1", uri=True
        )
        integrity_rows = [str(row[0]).lower() for row in db.execute("PRAGMA integrity_check")]
        integrity = "ok" if integrity_rows == ["ok"] else "; ".join(integrity_rows)
        table_columns = {
            table: {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            for table in _REQUIRED_SCHEMA
        }
        revision_row = (
            db.execute("SELECT version_num FROM alembic_version").fetchone()
            if table_columns["alembic_version"]
            else None
        )
        revision = str(revision_row[0]) if revision_row else None
        schema_valid = revision == _SCHEMA_REVISION and all(
            required <= table_columns[table]
            for table, required in _REQUIRED_SCHEMA.items()
        )
    except sqlite3.DatabaseError as exc:
        raise ValueError("backup is not a readable SQLite database") from exc
    finally:
        if db is not None:
            db.close()
    return BackupVerification(candidate, checksum, integrity, schema_valid, revision)


def _record_audit(session: Session | None, event_type: str, details: dict[str, Any]) -> None:
    if session is None:
        return
    session.add(
        AuditEvent(
            event_type=event_type,
            entity_type="backup",
            details_json=json.dumps(details, ensure_ascii=False, sort_keys=True),
        )
    )
    session.flush()


def _verified_backups(directory: Path) -> list[Path]:
    verified: list[Path] = []
    for path in directory.glob("finance-*.sqlite3"):
        try:
            result = verify_backup(path, require_checksum=True)
        except (OSError, ValueError):
            continue
        if result.integrity_check == "ok" and result.schema_valid:
            verified.append(path)
    return sorted(
        verified,
        key=lambda item: (item.stat().st_mtime_ns, item.name),
        reverse=True,
    )


def _retain_verified(directory: Path, keep: int) -> None:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    for path in _verified_backups(directory)[keep:]:
        path.unlink(missing_ok=True)
        _sidecar(path).unlink(missing_ok=True)


def _stage_retention(directory: Path, keep: int, current: Path) -> tuple[Path, list[tuple[Path, Path]]]:
    candidates = _verified_backups(directory)[keep:]
    if not candidates:
        return directory / f".retention-{uuid.uuid4().hex}", []
    quarantine = directory / f".retention-{uuid.uuid4().hex}"
    quarantine.mkdir()
    moved: list[tuple[Path, Path]] = []
    try:
        for path in candidates:
            if path == current:
                continue
            for original in (path, _sidecar(path)):
                if original.exists():
                    staged = quarantine / original.name
                    original.replace(staged)
                    moved.append((original, staged))
    except Exception:
        for original, staged in reversed(moved):
            staged.replace(original)
        quarantine.rmdir()
        raise
    return quarantine, moved


def create_backup(
    source: Any | None = None,
    directory: str | Path | None = None,
    *,
    keep: int = 14,
    session: Session | None = None,
    clock=utc_now,
) -> Path:
    """Create and verify an online SQLite backup, retaining the newest copies."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    if isinstance(source, Session) and session is None:
        session = source
    live_path = _database_path(source)
    if not live_path.exists():
        raise FileNotFoundError(live_path)
    target_dir = Path(directory or "./data/backups").expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"finance-{stamp}.sqlite3"
    suffix = 1
    while target.exists():
        target = target_dir / f"finance-{stamp}-{suffix}.sqlite3"
        suffix += 1
    temporary = target.with_suffix(".sqlite3.tmp")
    temporary_sidecar = target_dir / f".{target.name}.{uuid.uuid4().hex}.sha256.tmp"
    published = False
    quarantine: Path | None = None
    moved: list[tuple[Path, Path]] = []
    try:
        source_db = sqlite3.connect(live_path)
        target_db = sqlite3.connect(temporary)
        try:
            source_db.backup(target_db)
            target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target_db.commit()
            target_db.execute("PRAGMA journal_mode=DELETE")
        finally:
            target_db.close()
            source_db.close()
        verification = verify_backup(temporary)
        if verification.integrity_check != "ok" or not verification.schema_valid:
            raise ValueError("new backup failed integrity or schema validation")
        checksum = verification.checksum
        temporary_sidecar.write_text(f"{checksum}  {target.name}\n", encoding="ascii")
        temporary.replace(target)
        published = True
        temporary_sidecar.replace(_sidecar(target))
        quarantine, moved = _stage_retention(target_dir, keep, target)
        _record_audit(
            session,
            "backup.created",
            {"path": str(target), "sha256": checksum, "integrity_check": "ok"},
        )
        if session is not None:
            session.commit()
        if quarantine.exists():
            shutil.rmtree(quarantine, ignore_errors=True)
        return target
    except Exception:
        if session is not None and hasattr(session, "rollback"):
            session.rollback()
        for original, staged in reversed(moved):
            if staged.exists():
                staged.replace(original)
        if quarantine is not None and quarantine.exists():
            shutil.rmtree(quarantine, ignore_errors=True)
        if published:
            target.unlink(missing_ok=True)
            _sidecar(target).unlink(missing_ok=True)
            _remove_sqlite_auxiliary_files(target)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
        _remove_sqlite_auxiliary_files(temporary)


def restore_check(path: str | Path, *, session: Session | None = None) -> BackupVerification:
    """Validate a candidate in an isolated temporary copy; never touch live data."""
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    with tempfile.TemporaryDirectory(prefix="finance-restore-") as work:
        copied = Path(work) / candidate.name
        shutil.copyfile(candidate, copied)
        sidecar = _sidecar(candidate)
        if sidecar.exists():
            shutil.copyfile(sidecar, _sidecar(copied))
        result = verify_backup(copied, require_checksum=True)
        if not result.schema_valid or result.integrity_check != "ok":
            raise ValueError("backup schema or integrity check failed")
    _record_audit(
        session,
        "backup.restore_checked",
        {"path": str(candidate), "sha256": result.checksum, "integrity_check": "ok"},
    )
    if session is not None:
        session.commit()
    return BackupVerification(
        candidate,
        result.checksum,
        result.integrity_check,
        result.schema_valid,
        result.schema_revision,
    )
