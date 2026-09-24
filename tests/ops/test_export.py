from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from finance_app.db import create_db_engine, get_session_factory
from finance_app.ledger.models import AuditEvent
from finance_app.ops.export import ExportService


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "finance.db"
    monkeypatch.setenv("FINANCE_DATABASE_URL", f"sqlite:///{path}")
    command.upgrade(Config("alembic.ini"), "head")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO users(username,password_hash,is_active,created_at,updated_at) VALUES ('admin','password-secret',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES ('daily_schedule','09:00',CURRENT_TIMESTAMP)")
        for key in ("api_key", "private_key", "smtp_pass", "pushplus_token"):
            db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)", (key, f"{key}-secret"))
        db.execute("INSERT INTO accounts(name,kind,currency,opening_balance_cents,created_at,updated_at) VALUES ('cash','cash','CNY',100,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO assets(code,market,name,asset_class,risk_level,portfolio_role,currency,created_at,updated_at) VALUES ('000001','CN','fund','fund','medium','core','CNY',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO holdings(account_id,asset_id,quantity,cost_cents,is_price_stale,updated_at) VALUES (1,1,1,100,1,CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO transactions(source,external_id,kind,account_id,asset_id,amount_cents,quantity,fee_cents,occurred_at,created_at) VALUES ('manual','tx-1','BUY',1,1,100,1,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO audit_events(event_type,details_json,created_at) VALUES ('unsafe','{\"api_key\":\"audit-secret\"}',CURRENT_TIMESTAMP)")
        db.commit()
    return path


def test_json_export_keeps_business_tables_but_excludes_secrets(populated_db: Path):
    payload = ExportService(populated_db).to_json()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["version"] == 1
    assert {"accounts", "assets", "holdings", "transactions"} <= set(payload["tables"])
    assert [row["key"] for row in payload["tables"]["app_settings"]] == ["daily_schedule"]
    for secret in ("password-secret", "api_key-secret", "private_key-secret", "smtp_pass-secret", "pushplus_token-secret", "audit-secret"):
        assert secret not in encoded
    assert "password_hash" not in encoded


def test_export_all_writes_single_bom_and_single_audit(populated_db: Path, tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{populated_db}")
    session = get_session_factory(engine)()
    try:
        paths = ExportService(populated_db, session=session).export_all(tmp_path / "exports")
        assert paths["holdings"].read_bytes().startswith(b"\xef\xbb\xbf")
        assert not paths["holdings"].read_bytes().startswith(b"\xef\xbb\xbf\xef\xbb\xbf")
        assert "BUY" in paths["transactions"].read_text(encoding="utf-8-sig")
        events = list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == "export.completed")))
        assert len(events) == 1
    finally:
        session.close()
        engine.dispose()


def test_export_failure_leaves_existing_set_untouched(
    populated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "exports"
    directory.mkdir()
    old = {name: f"old-{name}" for name in ("holdings.csv", "transactions.csv", "portfolio.json")}
    for name, value in old.items():
        (directory / name).write_text(value, encoding="utf-8")
    service = ExportService(populated_db)

    def fail_json(*args, **kwargs):
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(service, "_json_payload", fail_json)
    with pytest.raises(RuntimeError, match="serialization"):
        service.export_all(directory)
    assert {name: (directory / name).read_text(encoding="utf-8") for name in old} == old
