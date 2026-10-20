"""app_metric_samples table

Revision ID: d2f6a8c0e4b9
Revises: b4e7a2c9d1f6
Create Date: 2026-10-20 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd2f6a8c0e4b9'
down_revision: Union[str, None] = 'b4e7a2c9d1f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_metric_samples',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('apps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('cpu_m', sa.Integer(), nullable=False),
        sa.Column('mem_mi', sa.Integer(), nullable=False),
    )
    op.create_index('ix_app_metric_samples_app_id', 'app_metric_samples', ['app_id'])


def downgrade() -> None:
    op.drop_index('ix_app_metric_samples_app_id', table_name='app_metric_samples')
    op.drop_table('app_metric_samples')
