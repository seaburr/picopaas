"""apps.error_count / last_attempt_at / error_retryable (reconciler retry with backoff)

Revision ID: b7e2d9a4c1f3
Revises: a1b2c3d4e5f6
Create Date: 2026-10-19 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b7e2d9a4c1f3'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('error_count', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('last_attempt_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('error_retryable', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('error_retryable')
        batch.drop_column('last_attempt_at')
        batch.drop_column('error_count')
