"""apps.status_reason, deploying_since, restart_nonce

Revision ID: f1a8c4d7e2b5
Revises: a9f1c2d3e4b5
Create Date: 2026-10-19 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f1a8c4d7e2b5'
down_revision: Union[str, None] = 'a9f1c2d3e4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('status_reason', sa.String(length=32), nullable=True))
        batch.add_column(sa.Column('deploying_since', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('restart_nonce', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('restart_nonce')
        batch.drop_column('deploying_since')
        batch.drop_column('status_reason')
