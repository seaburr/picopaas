"""api_keys.name (optional label; last_used_at already exists)

Revision ID: c5d8e2f9a3b7
Revises: b4c7d1e8f2a6
Create Date: 2026-10-20 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c5d8e2f9a3b7'
down_revision: Union[str, None] = 'b4c7d1e8f2a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('api_keys') as batch:
        batch.add_column(sa.Column('name', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('api_keys') as batch:
        batch.drop_column('name')
