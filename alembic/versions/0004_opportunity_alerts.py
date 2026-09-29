"""Record opportunity alerts once per fund and salary cycle."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("cycle_start", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("data_as_of", sa.DateTime(), nullable=False),
        sa.Column("used_special_budget", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "code", "cycle_start", name="uq_opportunity_alerts_code_cycle"
        ),
    )


def downgrade() -> None:
    op.drop_table("opportunity_alerts")
