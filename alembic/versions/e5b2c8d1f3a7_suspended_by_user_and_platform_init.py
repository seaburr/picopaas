"""app.suspended_by_user and platform_init singleton

Revision ID: e5b2c8d1f3a7
Revises: d4e1a7b9c2f6
Create Date: 2026-10-19 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e5b2c8d1f3a7'
down_revision: Union[str, None] = 'd4e1a7b9c2f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('apps', sa.Column('suspended_by_user', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('platform_init',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id'))
    # Existing deployments already have users, so the first-user slot is taken.
    op.execute("INSERT INTO platform_init (id) SELECT 1 WHERE EXISTS (SELECT 1 FROM users)"
               if op.get_bind().dialect.name == "sqlite" else
               "INSERT INTO platform_init (id) SELECT 1 FROM DUAL WHERE EXISTS (SELECT 1 FROM users)")


def downgrade() -> None:
    op.drop_table('platform_init')
    op.drop_column('apps', 'suspended_by_user')
