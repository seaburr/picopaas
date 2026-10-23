"""per-app persistent volume (size, mount path, PVC phase)

Revision ID: b6d3f1a8c2e4
Revises: a7c1e9b3d5f2
Create Date: 2026-10-23 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b6d3f1a8c2e4'
down_revision: Union[str, None] = 'a7c1e9b3d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('volume_size_gi', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('volume_mount_path', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('volume_phase', sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('volume_phase')
        batch.drop_column('volume_mount_path')
        batch.drop_column('volume_size_gi')
