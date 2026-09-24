from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from finance_app.ops.export import ExportService


def test_json_export_contains_provenance_but_no_password_hash(tmp_path: Path):
    database = tmp_path / "finance.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)")
        db.execute("INSERT INTO users VALUES (1, 'admin', 'secret-hash')")
        db.execute("CREATE TABLE app_settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        db.execute("INSERT INTO app_settings VALUES (1, 'pushplus_token', 'secret-token')")
        db.commit()
    service = ExportService(database)
    payload = service.to_json()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "source_timestamp" in payload
    assert "password_hash" not in encoded
    assert "secret-token" not in encoded


def test_csv_exports_are_utf8_bom_and_include_holdings_and_transactions(tmp_path: Path):
    database = tmp_path / "finance.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE holdings (id INTEGER PRIMARY KEY, code TEXT)")
        db.execute("INSERT INTO holdings VALUES (1, '000001')")
        db.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, kind TEXT)")
        db.execute("INSERT INTO transactions VALUES (1, 'BUY')")
        db.commit()
    service = ExportService(database)
    holdings = service.holdings_csv()
    transactions = service.transactions_csv()
    assert holdings.startswith("\ufeff")
    assert "000001" in holdings
    assert transactions.startswith("\ufeff")
    assert "BUY" in transactions
