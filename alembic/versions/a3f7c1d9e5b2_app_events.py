"""app_events table (recent restart events per app)

Revision ID: a3f7c1d9e5b2
Revises: c5d8e2f9a3b7
Create Date: 2026-10-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3f7c1d9e5b2'
down_revision: Union[str, None] = 'c5d8e2f9a3b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('apps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('message', sa.String(length=255), nullable=False),
        sa.Column('dedupe_key', sa.String(length=160), nullable=False),
        sa.UniqueConstraint('app_id', 'dedupe_key', name='uq_app_events_dedupe'),
    )
    op.create_index('ix_app_events_app_id', 'app_events', ['app_id'])


def downgrade() -> None:
    op.drop_index('ix_app_events_app_id', table_name='app_events')
    op.drop_table('app_events')
