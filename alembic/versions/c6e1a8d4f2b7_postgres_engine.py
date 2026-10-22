"""db_servers.engine + apps.db_engine (managed PostgreSQL alongside MySQL)

Revision ID: c6e1a8d4f2b7
Revises: b7d2e4f6a8c1
Create Date: 2026-10-22 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c6e1a8d4f2b7'
down_revision: Union[str, None] = 'b7d2e4f6a8c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('db_servers') as batch:
        batch.add_column(sa.Column('engine', sa.String(length=8), nullable=False, server_default='mysql'))
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('db_engine', sa.String(length=8), nullable=False, server_default='mysql'))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('db_engine')
    with op.batch_alter_table('db_servers') as batch:
        batch.drop_column('engine')
