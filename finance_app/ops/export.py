from __future__ import annotations

import csv
import io
import json
import shutil
import sqlite3
import tempfile
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from finance_app.db import utc_now
from finance_app.ledger.models import AuditEvent
from finance_app.ops.backup import _database_path

_EXPORT_FIELDS: dict[str, tuple[str, ...]] = {
    "accounts": ("id", "name", "kind", "currency", "opening_balance_cents", "created_at", "updated_at"),
    "alerts": ("id", "alert_type", "severity", "message", "status", "created_at", "resolved_at"),
    "allocation_targets": ("id", "name", "target_bps", "upper_bps", "created_at", "updated_at"),
    "assets": ("id", "code", "market", "name", "asset_class", "risk_level", "portfolio_role", "currency", "created_at", "updated_at"),
    "audit_events": ("id", "event_type", "entity_type", "entity_id", "created_at"),
    "cash_buckets": ("id", "bucket_kind", "balance_cents", "updated_at"),
    "holdings": ("id", "account_id", "asset_id", "quantity", "cost_cents", "valuation_cents", "last_price", "price_source", "price_fetched_at", "is_price_stale", "updated_at"),
    "import_batches": ("id", "sha256", "filename", "source", "imported_at"),
    "job_runs": ("id", "job_name", "business_date", "status", "started_at", "completed_at", "error_summary"),
    "monthly_budgets": ("id", "month", "bucket_kind", "amount_cents", "created_at"),
    "notification_channels": ("id", "name", "channel_type", "enabled", "created_at", "updated_at"),
    "notification_deliveries": ("id", "channel_id", "title", "status", "attempt_count", "error_summary", "delivered_at", "created_at"),
    "portfolio_snapshots": ("id", "snapshot_date", "total_value_cents", "known_value_cents", "data_complete", "details_json", "invested_value_cents", "cash_value_cents", "created_at"),
    "price_snapshots": ("id", "asset_id", "valuation_date", "source", "price", "source_url", "fetched_at", "error_text"),
    "transactions": ("id", "source", "external_id", "kind", "account_id", "asset_id", "amount_cents", "quantity", "price", "fee_cents", "occurred_at", "description", "reverses_transaction_id", "created_at"),
    "users": ("id", "username", "is_active", "created_at", "updated_at"),
}
_SAFE_SETTING_KEYS = {"daily_schedule", "reserve_target_cents", "stale_threshold_hours"}


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


class ExportService:
    """Produce a consistent, portable and explicitly non-secret export set."""

    def __init__(self, source: Any | None = None, *, session: Session | None = None, clock=utc_now):
        if isinstance(source, Session) and session is None:
            session = source
        self.session = session
        self.database = _database_path(source if source is not None else session)
        self.clock = clock

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    @staticmethod
    def _rows(db: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> list[dict[str, Any]]:
        available = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        selected = tuple(column for column in columns if column in available)
        if not selected:
            return []
        query = ",".join(f'"{column}"' for column in selected)
        return [
            {column: _json_value(value) for column, value in zip(selected, row)}
            for row in db.execute(f'SELECT {query} FROM "{table}"')
        ]

    def _json_payload(self, snapshot: Path) -> dict[str, Any]:
        db = self._connect(snapshot)
        try:
            tables = {
                table: self._rows(db, table, columns)
                for table, columns in _EXPORT_FIELDS.items()
            }
            settings = self._rows(db, "app_settings", ("id", "key", "value", "updated_at"))
        finally:
            db.close()
        tables["app_settings"] = [row for row in settings if row.get("key") in _SAFE_SETTING_KEYS]
        return {"version": 1, "source_timestamp": self.clock().isoformat(), "tables": tables}

    @staticmethod
    def _csv(snapshot: Path, table: str) -> str:
        columns = _EXPORT_FIELDS[table]
        db = ExportService._connect(snapshot)
        try:
            rows = ExportService._rows(db, table, columns)
        finally:
            db.close()
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
        return "\ufeff" + output.getvalue()

    def _snapshot(self, directory: Path) -> Path:
        snapshot = directory / "snapshot.sqlite3"
        source_db = sqlite3.connect(self.database)
        target_db = sqlite3.connect(snapshot)
        try:
            source_db.backup(target_db)
            target_db.commit()
        finally:
            target_db.close()
            source_db.close()
        return snapshot

    def holdings_csv(self) -> str:
        return self._csv(self.database, "holdings")

    def transactions_csv(self) -> str:
        return self._csv(self.database, "transactions")

    def to_json(self) -> dict[str, Any]:
        return self._json_payload(self.database)

    def export_json(self, path: str | Path) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return destination

    def export_all(self, directory: str | Path) -> dict[str, Path]:
        destination = Path(directory).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        names = {"holdings": "holdings.csv", "transactions": "transactions.csv", "json": "portfolio.json"}
        with tempfile.TemporaryDirectory(prefix=".finance-export-", dir=destination.parent) as work:
            staging = Path(work)
            snapshot = self._snapshot(staging)
            (staging / names["holdings"]).write_text(self._csv(snapshot, "holdings"), encoding="utf-8", newline="")
            (staging / names["transactions"]).write_text(self._csv(snapshot, "transactions"), encoding="utf-8", newline="")
            (staging / names["json"]).write_text(
                json.dumps(self._json_payload(snapshot), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            destination.mkdir(exist_ok=True)
            rollback = destination.parent / f".finance-export-rollback-{uuid.uuid4().hex}"
            rollback.mkdir()
            moved_old: list[tuple[Path, Path]] = []
            published: list[Path] = []
            try:
                for name in names.values():
                    final = destination / name
                    if final.exists():
                        saved = rollback / name
                        final.replace(saved)
                        moved_old.append((final, saved))
                for name in names.values():
                    final = destination / name
                    (staging / name).replace(final)
                    published.append(final)
                if self.session is not None:
                    self.session.add(
                        AuditEvent(
                            event_type="export.completed",
                            entity_type="export",
                            details_json=json.dumps({"format": "csv+json", "directory": str(destination)}, sort_keys=True),
                        )
                    )
                    self.session.commit()
            except Exception:
                if self.session is not None:
                    self.session.rollback()
                for path in published:
                    path.unlink(missing_ok=True)
                for final, saved in reversed(moved_old):
                    saved.replace(final)
                raise
            finally:
                shutil.rmtree(rollback, ignore_errors=True)
        return {key: destination / name for key, name in names.items()}
