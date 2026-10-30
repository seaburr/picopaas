"""kaisen restore points (kaisen_snapshots)

Revision ID: a8c3e5f1b2d4
Revises: d7b2f4a9c1e5
Create Date: 2026-10-30 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'a8c3e5f1b2d4'
down_revision: Union[str, None] = 'd7b2f4a9c1e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kaisen_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('kaisen_builds.id', ondelete='CASCADE'), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('files_count', sa.Integer(), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('blob', sa.LargeBinary().with_variant(mysql.LONGBLOB(), 'mysql'), nullable=False),
    )
    op.create_index('ix_kaisen_snapshots_build_id', 'kaisen_snapshots', ['build_id'])


def downgrade() -> None:
    op.drop_index('ix_kaisen_snapshots_build_id', table_name='kaisen_snapshots')
    op.drop_table('kaisen_snapshots')
