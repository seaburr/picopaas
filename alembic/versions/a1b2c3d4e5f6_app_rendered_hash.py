"""apps.rendered_hash

Revision ID: a1b2c3d4e5f6
Revises: a7d4e0c3b9f1
Create Date: 2026-10-19 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'a7d4e0c3b9f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('rendered_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('rendered_hash')
