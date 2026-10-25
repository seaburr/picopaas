"""measured volume usage on apps (used bytes, when, stat Job in flight)

Revision ID: d1e5a7c3b9f2
Revises: c9d4f1a7b3e2
Create Date: 2026-10-24 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd1e5a7c3b9f2'
down_revision: Union[str, None] = 'c9d4f1a7b3e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('volume_used_bytes', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('volume_used_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('volume_stat_job', sa.String(length=63), nullable=True))
        batch.add_column(sa.Column('volume_stat_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('volume_stat_at')
        batch.drop_column('volume_stat_job')
        batch.drop_column('volume_used_at')
        batch.drop_column('volume_used_bytes')
