"""per-app outbound bandwidth override (Mbit/s)

Revision ID: e8b2c5d7a914
Revises: d4a7b9c1e3f5
Create Date: 2026-10-22 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e8b2c5d7a914'
down_revision: Union[str, None] = 'd4a7b9c1e3f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('egress_mbit', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('egress_mbit')
