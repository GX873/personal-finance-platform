"""Create the initial personal finance schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""

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
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
