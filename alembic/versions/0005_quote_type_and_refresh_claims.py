"""Keep official and intraday snapshots distinct and claim refreshes atomically."""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table rebuilds SQLite tables while preserving all existing rows;
    # PostgreSQL receives the equivalent constraint drop/create operations.
    with op.batch_alter_table("price_snapshots") as batch:
        batch.drop_constraint("uq_price_snapshots_asset_valuation_source", type_="unique")
        batch.create_unique_constraint(
            "uq_price_snapshots_asset_valuation_source",
            ["asset_id", "valuation_date", "source", "quote_type"],
        )

    op.create_table(
        "fund_refresh_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", name="uq_fund_refresh_claims_asset"),
    )


def downgrade() -> None:
    op.drop_table("fund_refresh_claims")
    # A downgrade can only succeed when no official/intraday duplicate keys
    # exist; this is intentional rather than silently deleting production data.
    with op.batch_alter_table("price_snapshots") as batch:
        batch.drop_constraint("uq_price_snapshots_asset_valuation_source", type_="unique")
        batch.create_unique_constraint(
            "uq_price_snapshots_asset_valuation_source",
            ["asset_id", "valuation_date", "source"],
        )
