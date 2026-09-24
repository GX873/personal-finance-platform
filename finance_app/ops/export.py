from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from finance_app.db import utc_now
from finance_app.ledger.models import AuditEvent
from finance_app.ops.backup import _database_path

_SECRET_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "authorization",
    "webhook",
    "sendkey",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


class ExportService:
    """Produce portable, non-secret exports from a SQLite database."""

    def __init__(self, source: Any | None = None, *, session: Session | None = None, clock=utc_now):
        if isinstance(source, Session) and session is None:
            session = source
        self.session = session
        self.database = _database_path(
            source if source is not None else (session if session is not None else None)
        )
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)

    @staticmethod
    def _table_rows(db: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
        try:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
        except sqlite3.DatabaseError:
            return [], []
        if not columns:
            return [], []
        quoted = '"' + table.replace('"', '""') + '"'
        rows = list(db.execute(f"SELECT * FROM {quoted}"))
        return columns, rows

    def _audit(self, event_type: str, details: dict[str, Any]) -> None:
        if self.session is None:
            return
        self.session.add(
            AuditEvent(
                event_type=event_type,
                entity_type="export",
                details_json=json.dumps(details, ensure_ascii=False, sort_keys=True),
            )
        )
        self.session.commit()

    def _csv(self, table: str) -> str:
        with self._connect() as db:
            columns, rows = self._table_rows(db, table)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(columns)
        writer.writerows(rows)
        return "\ufeff" + output.getvalue()

    def holdings_csv(self) -> str:
        value = self._csv("holdings")
        self._audit("export.holdings", {"format": "csv", "rows": max(value.count("\n") - 1, 0)})
        return value

    def transactions_csv(self) -> str:
        value = self._csv("transactions")
        self._audit("export.transactions", {"format": "csv", "rows": max(value.count("\n") - 1, 0)})
        return value

    def _safe_rows(self, db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        columns, rows = self._table_rows(db, table)
        safe_columns = [
            column
            for column in columns
            if not any(part in column.lower() for part in _SECRET_KEY_PARTS)
        ]
        indexes = [columns.index(column) for column in safe_columns]
        return [
            {column: _json_value(row[index]) for column, index in zip(safe_columns, indexes)}
            for row in rows
        ]

    def to_json(self) -> dict[str, Any]:
        with self._connect() as db:
            table_names = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            tables: dict[str, list[dict[str, Any]]] = {}
            for table in table_names:
                rows = self._safe_rows(db, table)
                if rows or table not in {"users", "app_settings"}:
                    tables[table] = rows
        # App settings may contain secrets in the value column even when the key is benign.
        settings = tables.get("app_settings", [])
        tables["app_settings"] = [
            row
            for row in settings
            if not any(
                part in str(row.get("key", "")).lower() for part in _SECRET_KEY_PARTS
            )
        ]
        payload = {
            "version": 1,
            "source_timestamp": self.clock().isoformat(),
            "tables": tables,
        }
        return payload

    def export_json(self, path: str | Path) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._audit("export.json", {"format": "json", "path": str(destination)})
        return destination

    def export_all(self, directory: str | Path) -> dict[str, Path]:
        destination = Path(directory).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        holdings = destination / "holdings.csv"
        transactions = destination / "transactions.csv"
        portfolio = destination / "portfolio.json"
        # The serializers include exactly one BOM; plain UTF-8 avoids adding a second one.
        holdings.write_text(self.holdings_csv(), encoding="utf-8", newline="")
        transactions.write_text(self.transactions_csv(), encoding="utf-8", newline="")
        self.export_json(portfolio)
        return {"holdings": holdings, "transactions": transactions, "json": portfolio}
