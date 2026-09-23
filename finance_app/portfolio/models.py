from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from finance_app.db import Base, UtcDateTime, utc_now
from finance_app.ledger.models import DECIMAL_24_8


class AllocationTarget(Base):
    __tablename__ = "allocation_targets"
    __table_args__ = (
        CheckConstraint(
            "target_bps BETWEEN 0 AND 10000", name="ck_allocation_targets_target_bps"
        ),
        CheckConstraint(
            "upper_bps IS NULL OR upper_bps BETWEEN 0 AND 10000",
            name="ck_allocation_targets_upper_bps",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    target_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    upper_bps: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "valuation_date",
            "source",
            name="uq_price_snapshots_asset_valuation_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[Decimal] = mapped_column(DECIMAL_24_8, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )
    error_text: Mapped[str | None] = mapped_column(Text)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", name="uq_portfolio_snapshots_snapshot_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_value_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invested_value_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    cash_value_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
