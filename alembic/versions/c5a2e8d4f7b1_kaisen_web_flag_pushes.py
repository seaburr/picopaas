"""projects.kaisen_web_search_enabled (default true) and kaisen_pushes (async GitHub push jobs)

Revision ID: c5a2e8d4f7b1
Revises: b3e7d1a9c5f2
Create Date: 2026-11-04 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c5a2e8d4f7b1'
down_revision: Union[str, None] = 'b3e7d1a9c5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('projects') as b:
        b.add_column(sa.Column('kaisen_web_search_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        'kaisen_pushes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('kaisen_builds.id'), nullable=False),
        sa.Column('started_by', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('error_code', sa.String(32), nullable=True),
        sa.Column('url', sa.String(500), nullable=True),
        sa.Column('pr_url', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_kaisen_pushes_build_id', 'kaisen_pushes', ['build_id'])


def downgrade() -> None:
    op.drop_index('ix_kaisen_pushes_build_id', table_name='kaisen_pushes')
    op.drop_table('kaisen_pushes')
    with op.batch_alter_table('projects') as b:
        b.drop_column('kaisen_web_search_enabled')
