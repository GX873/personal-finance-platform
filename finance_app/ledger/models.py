from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from finance_app.db import Base, UtcDateTime, utc_now

DECIMAL_24_8 = Numeric(24, 8)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PortfolioRole(StrEnum):
    CORE = "core"
    SATELLITE = "satellite"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    opening_balance_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("code", "market", name="uq_assets_code_market"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_class: Mapped[str | None] = mapped_column(String(64))
    risk_level: Mapped[RiskLevel] = mapped_column(
        String(16), nullable=False, default=RiskLevel.MEDIUM
    )
    portfolio_role: Mapped[PortfolioRole] = mapped_column(
        String(16), nullable=False, default=PortfolioRole.CORE
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", name="uq_transactions_source_external_id"
        ),
        CheckConstraint("fee_cents >= 0", name="ck_transactions_fee_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT")
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quantity: Mapped[Decimal] = mapped_column(
        DECIMAL_24_8, nullable=False, default=Decimal(0)
    )
    price: Mapped[Decimal | None] = mapped_column(DECIMAL_24_8)
    fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)
    reverses_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("account_id", "asset_id", name="uq_holdings_account_asset"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(
        DECIMAL_24_8, nullable=False, default=Decimal(0)
    )
    cost_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    valuation_cents: Mapped[int | None] = mapped_column(BigInteger)
    last_price: Mapped[Decimal | None] = mapped_column(DECIMAL_24_8)
    price_source: Mapped[str | None] = mapped_column(String(64))
    price_fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    is_price_stale: Mapped[bool] = mapped_column(default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class CashBucket(Base):
    __tablename__ = "cash_buckets"

    id: Mapped[int] = mapped_column(primary_key=True)
    bucket_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class MonthlyBudget(Base):
    __tablename__ = "monthly_budgets"
    __table_args__ = (
        UniqueConstraint(
            "month", "bucket_kind", name="uq_monthly_budgets_month_bucket_kind"
        ),
        CheckConstraint(
            "length(month) = 7 AND month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_monthly_budgets_month",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    bucket_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(128))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utc_now, nullable=False
    )
