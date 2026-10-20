"""apps.run_as_uid (forced runAsUser/runAsGroup for images with a named USER)

Revision ID: e7a3c9b1d5f2
Revises: d9e5f2a8b3c1
Create Date: 2026-10-19 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e7a3c9b1d5f2'
down_revision: Union[str, None] = 'd9e5f2a8b3c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('run_as_uid', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('run_as_uid')
