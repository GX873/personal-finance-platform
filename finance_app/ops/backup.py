from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
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
    if not candidate.is_file() or candidate.suffix != ".sqlite3":
        raise ValueError("backup path must be an existing .sqlite3 file")
    checksum = _sha256(candidate)
    expected = _read_expected_checksum(candidate)
    if expected is not None and checksum != expected:
        raise ValueError("backup checksum does not match sidecar")
    if require_checksum and expected is None:
        raise ValueError("backup checksum sidecar is missing")
    db = None
    try:
        db = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    except sqlite3.DatabaseError as exc:
        raise ValueError("backup is not a readable SQLite database") from exc
    finally:
        if db is not None:
            db.close()
    return BackupVerification(candidate, checksum, integrity, bool(tables))


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
    session.commit()


def _retain_verified(directory: Path, keep: int) -> None:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    verified: list[Path] = []
    for path in directory.glob("finance-*.sqlite3"):
        try:
            verify_backup(path, require_checksum=True)
        except (OSError, ValueError):
            continue
        verified.append(path)
    for path in sorted(verified, key=lambda item: item.name, reverse=True)[keep:]:
        path.unlink(missing_ok=True)
        _sidecar(path).unlink(missing_ok=True)


def create_backup(
    source: Any | None = None,
    directory: str | Path | None = None,
    *,
    keep: int = 14,
    session: Session | None = None,
    clock=utc_now,
) -> Path:
    """Create and verify an online SQLite backup, retaining the newest copies."""
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
    try:
        source_db = sqlite3.connect(live_path)
        target_db = sqlite3.connect(temporary)
        try:
            source_db.backup(target_db)
            target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target_db.commit()
        finally:
            target_db.close()
            source_db.close()
        temporary.replace(target)
        verification = verify_backup(target)
        if verification.integrity_check != "ok" or not verification.schema_valid:
            raise ValueError("new backup failed integrity or schema validation")
        checksum = verification.checksum
        _sidecar(target).write_text(f"{checksum}  {target.name}\n", encoding="ascii")
        _retain_verified(target_dir, keep)
        _record_audit(
            session,
            "backup.created",
            {"path": str(target), "sha256": checksum, "integrity_check": "ok"},
        )
        return target
    finally:
        temporary.unlink(missing_ok=True)


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
    return BackupVerification(candidate, result.checksum, result.integrity_check, result.schema_valid)
