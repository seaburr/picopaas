"""users.timezone (IANA name for display; NULL = browser timezone)

Revision ID: a7c2e9f4d6b1
Revises: d1e5a7c3b9f2
Create Date: 2026-10-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7c2e9f4d6b1'
down_revision: Union[str, None] = 'd1e5a7c3b9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('timezone', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('timezone')
