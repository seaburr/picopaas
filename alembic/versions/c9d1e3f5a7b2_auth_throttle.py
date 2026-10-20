"""auth_throttle table for login/register rate limiting

Revision ID: c9d1e3f5a7b2
Revises: b4c8e2f6a1d3
Create Date: 2026-10-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c9d1e3f5a7b2'
down_revision: Union[str, None] = 'b4c8e2f6a1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'auth_throttle',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('window_start', sa.DateTime(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index('ix_auth_throttle_window_start', 'auth_throttle', ['window_start'])


def downgrade() -> None:
    op.drop_index('ix_auth_throttle_window_start', table_name='auth_throttle')
    op.drop_table('auth_throttle')
