"""app_image_history table, apps.rejected_digest

Revision ID: b4e7a2c9d1f6
Revises: c9d1e3f5a7b2
Create Date: 2026-10-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b4e7a2c9d1f6'
down_revision: Union[str, None] = 'c9d1e3f5a7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('rejected_digest', sa.String(length=80), nullable=True))
    op.create_table(
        'app_image_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('apps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('digest', sa.String(length=80), nullable=False),
        sa.Column('image_ref', sa.String(length=512), nullable=False),
        sa.Column('run_as_uid', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_app_image_history_app_id', 'app_image_history', ['app_id'])


def downgrade() -> None:
    op.drop_index('ix_app_image_history_app_id', table_name='app_image_history')
    op.drop_table('app_image_history')
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('rejected_digest')
