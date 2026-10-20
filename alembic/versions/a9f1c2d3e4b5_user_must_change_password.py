"""users.must_change_password (set by admin password reset)

Revision ID: a9f1c2d3e4b5
Revises: e7a3c9b1d5f2
Create Date: 2026-10-19 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a9f1c2d3e4b5'
down_revision: Union[str, None] = 'e7a3c9b1d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.text('0')))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('must_change_password')
