from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from finance_app.ops.backup import create_backup, restore_check, verify_backup


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    path = tmp_path / "finance.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE holdings (id INTEGER PRIMARY KEY, code TEXT)")
        db.execute("INSERT INTO holdings(code) VALUES ('000001')")
        db.commit()
    return path


def test_online_backup_opens_and_passes_integrity_check(populated_db: Path, tmp_path: Path):
    backup = create_backup(populated_db, tmp_path / "backups", keep=14)
    assert backup.name.startswith("finance-")
    assert backup.suffix == ".sqlite3"
    assert verify_backup(backup).integrity_check == "ok"
    assert (backup.with_name(backup.name + ".sha256")).exists()


def test_backup_retains_only_newest_verified_files(populated_db: Path, tmp_path: Path):
    directory = tmp_path / "backups"
    directory.mkdir()
    for index in range(16):
        path = directory / f"finance-20260101-{index:06d}.sqlite3"
        path.write_bytes(populated_db.read_bytes())
        path.with_name(path.name + ".sha256").write_text(
            hashlib.sha256(path.read_bytes()).hexdigest(), encoding="ascii"
        )
    create_backup(populated_db, directory, keep=14)
    assert len(list(directory.glob("finance-*.sqlite3"))) == 14


def test_restore_check_uses_temp_copy_and_rejects_bad_checksum(
    populated_db: Path, tmp_path: Path
):
    backup = create_backup(populated_db, tmp_path / "backups")
    result = restore_check(backup)
    assert result.integrity_check == "ok"
    assert populated_db.read_bytes() != b""

    backup.with_name(backup.name + ".sha256").write_text("0" * 64, encoding="ascii")
    with pytest.raises(ValueError, match="checksum"):
        restore_check(backup)
