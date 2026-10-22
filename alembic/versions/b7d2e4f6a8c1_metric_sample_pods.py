"""metric samples: pod count + per-pod usage

Revision ID: b7d2e4f6a8c1
Revises: f23a7cb649f7
Create Date: 2026-10-22 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d2e4f6a8c1'
down_revision: Union[str, None] = 'f23a7cb649f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('app_metric_samples', sa.Column('pods', sa.Integer(), server_default='1', nullable=False))
    op.add_column('app_metric_samples', sa.Column('per_pod', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('app_metric_samples', 'per_pod')
    op.drop_column('app_metric_samples', 'pods')
