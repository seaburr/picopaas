"""app replicas

Revision ID: f23a7cb649f7
Revises: a5e9c1d7b3f2
Create Date: 2026-10-22 06:15:48.476080

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f23a7cb649f7'
down_revision: Union[str, None] = 'a5e9c1d7b3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('apps', sa.Column('replicas', sa.Integer(), server_default='1', nullable=False))


def downgrade() -> None:
    op.drop_column('apps', 'replicas')
