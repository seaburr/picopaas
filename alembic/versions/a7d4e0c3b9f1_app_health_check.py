"""app health check settings

Revision ID: a7d4e0c3b9f1
Revises: f6c3d9e2a4b8
Create Date: 2026-10-19 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7d4e0c3b9f1'
down_revision: Union[str, None] = 'f6c3d9e2a4b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('health_check_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('health_check_path', sa.String(length=255), nullable=False, server_default='/healthz'))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('health_check_path')
        batch.drop_column('health_check_enabled')
