"""Retain partial valuations and their provenance without claiming a total."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("portfolio_snapshots") as batch:
        batch.alter_column(
            "total_value_cents", existing_type=sa.BigInteger(), nullable=True
        )
        batch.add_column(
            sa.Column(
                "known_value_cents", sa.BigInteger(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column(
                "data_complete", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("details_json", sa.Text(), nullable=True))
    op.execute(
        "UPDATE portfolio_snapshots SET known_value_cents = total_value_cents, total_value_cents = NULL"
    )
    with op.batch_alter_table("allocation_targets") as batch:
        batch.create_check_constraint(
            "ck_allocation_targets_order",
            "upper_bps IS NULL OR target_bps <= upper_bps",
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM portfolio_snapshots WHERE total_value_cents IS NULL"
        )
    ):
        raise RuntimeError("cannot discard incomplete snapshot semantics")
    with op.batch_alter_table("allocation_targets") as batch:
        batch.drop_constraint("ck_allocation_targets_order", type_="check")
    with op.batch_alter_table("portfolio_snapshots") as batch:
        batch.drop_column("details_json")
        batch.drop_column("data_complete")
        batch.drop_column("known_value_cents")
        batch.alter_column(
            "total_value_cents", existing_type=sa.BigInteger(), nullable=False
        )
