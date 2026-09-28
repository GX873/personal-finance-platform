"""Classify stored price snapshots as official NAV or intraday estimates."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("price_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "quote_type",
                sa.String(length=32),
                nullable=False,
                server_default="official_nav",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("price_snapshots") as batch:
        batch.drop_column("quote_type")
