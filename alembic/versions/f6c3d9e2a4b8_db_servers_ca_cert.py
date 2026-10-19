"""db_servers.ca_cert

Revision ID: f6c3d9e2a4b8
Revises: e5b2c8d1f3a7
Create Date: 2026-10-19 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f6c3d9e2a4b8'
down_revision: Union[str, None] = 'e5b2c8d1f3a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('db_servers') as batch:
        batch.add_column(sa.Column('ca_cert', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('db_servers') as batch:
        batch.drop_column('ca_cert')
