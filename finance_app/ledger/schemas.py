"""Immutable commands for the append-only ledger."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class PostTransaction:
    source: str
    external_id: str
    kind: str
    account_id: int
    amount_cents: int
    occurred_at: datetime
    asset_id: int | None = None
    quantity: Decimal = Decimal(0)
    price: Decimal | None = None
    fee_cents: int = 0
    note: str | None = None
