"""Create the initial personal finance schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""

from sqlalchemy import MetaData, PrimaryKeyConstraint

from alembic import op
from finance_app.auth import models as auth_models  # noqa: F401
from finance_app.db import Base
from finance_app.ledger import models as ledger_models  # noqa: F401
from finance_app.notifications import models as notification_models  # noqa: F401
from finance_app.portfolio import models as portfolio_models  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration_metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(migration_metadata)
    for table in migration_metadata.sorted_tables:
        constraints = [
            constraint
            for constraint in list(table.constraints)
            if not isinstance(constraint, PrimaryKeyConstraint)
        ]
        op.create_table(table.name, *list(table.columns), *constraints)


def downgrade() -> None:
    for table in reversed(Base.metadata.sorted_tables):
        op.drop_table(table.name)
