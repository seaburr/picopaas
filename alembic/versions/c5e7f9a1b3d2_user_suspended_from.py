"""users.suspended_from: the status a user had when suspended, so unsuspend returns a pending signup to pending

Revision ID: c5e7f9a1b3d2
Revises: b9d4e6a2c8f1
Create Date: 2026-10-31

Existing suspended users keep NULL, which unsuspend treats as 'active' (the old behaviour): there is no record of
whether they were approved before.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c5e7f9a1b3d2'
down_revision: Union[str, None] = 'b9d4e6a2c8f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("suspended_from", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("suspended_from")
