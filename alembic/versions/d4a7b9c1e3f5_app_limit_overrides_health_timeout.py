"""per-app admin limit overrides + health check timeout

Revision ID: d4a7b9c1e3f5
Revises: c6e1a8d4f2b7
Create Date: 2026-10-22 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4a7b9c1e3f5'
down_revision: Union[str, None] = 'c6e1a8d4f2b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ('db_max_connections', 'ephemeral_limit_mi', 'db_quota_mb', 'storage_quota_mb', 'image_size_mb',
           'health_check_timeout_seconds')


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        for c in COLUMNS:
            batch.add_column(sa.Column(c, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        for c in reversed(COLUMNS):
            batch.drop_column(c)
