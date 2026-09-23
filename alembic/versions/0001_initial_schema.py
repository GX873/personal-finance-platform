"""Create the initial personal finance schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # UTC timestamps use naive storage; timezone awareness is restored by the ORM.
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("opening_balance_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "allocation_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("target_bps", sa.Integer(), nullable=False),
        sa.Column("upper_bps", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "target_bps BETWEEN 0 AND 10000", name="ck_allocation_targets_target_bps"
        ),
        sa.CheckConstraint(
            "upper_bps IS NULL OR upper_bps BETWEEN 0 AND 10000",
            name="ck_allocation_targets_upper_bps",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("asset_class", sa.String(length=64), nullable=True),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("portfolio_role", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", "market", name="uq_assets_code_market"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=128), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cash_buckets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bucket_kind", sa.String(length=64), nullable=False),
        sa.Column("balance_cents", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256"),
    )
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_name", "business_date", name="uq_job_runs_job_name_business_date"
        ),
    )
    op.create_table(
        "monthly_budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("bucket_kind", sa.String(length=64), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(month) = 7 AND month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_monthly_budgets_month",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "month", "bucket_kind", name="uq_monthly_budgets_month_bucket_kind"
        ),
    )
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("channel_type", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_value_cents", sa.BigInteger(), nullable=False),
        sa.Column("invested_value_cents", sa.BigInteger(), nullable=False),
        sa.Column("cash_value_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_date", name="uq_portfolio_snapshots_snapshot_date"
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("cost_cents", sa.BigInteger(), nullable=False),
        sa.Column("valuation_cents", sa.BigInteger(), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("price_source", sa.String(length=64), nullable=True),
        sa.Column("price_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("is_price_stale", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "asset_id", name="uq_holdings_account_asset"),
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["notification_channels.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("valuation_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "asset_id",
            "valuation_date",
            "source",
            name="uq_price_snapshots_asset_valuation_source",
        ),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reverses_transaction_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("fee_cents >= 0", name="ck_transactions_fee_nonnegative"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reverses_transaction_id"], ["transactions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source", "external_id", name="uq_transactions_source_external_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("transactions")
    op.drop_table("price_snapshots")
    op.drop_table("notification_deliveries")
    op.drop_table("holdings")
    op.drop_table("users")
    op.drop_table("portfolio_snapshots")
    op.drop_table("notification_channels")
    op.drop_table("monthly_budgets")
    op.drop_table("job_runs")
    op.drop_table("import_batches")
    op.drop_table("cash_buckets")
    op.drop_table("audit_events")
    op.drop_table("assets")
    op.drop_table("app_settings")
    op.drop_table("allocation_targets")
    op.drop_table("alerts")
    op.drop_table("accounts")
