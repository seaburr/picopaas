"""kaisen web access (settings), run suggestions, event web_searches

Revision ID: b5d8f1a3c6e2
Revises: a9c4e2b7d1f3
Create Date: 2026-10-30 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b5d8f1a3c6e2'
down_revision: Union[str, None] = 'a9c4e2b7d1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('kaisen_settings') as b:
        b.add_column(sa.Column('web_access', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('web_allowed_domains', sa.JSON(), nullable=True))
    with op.batch_alter_table('kaisen_runs') as b:
        b.add_column(sa.Column('suggestions', sa.JSON(), nullable=True))
    with op.batch_alter_table('kaisen_events') as b:
        b.add_column(sa.Column('web_searches', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('kaisen_events') as b:
        b.drop_column('web_searches')
    with op.batch_alter_table('kaisen_runs') as b:
        b.drop_column('suggestions')
    with op.batch_alter_table('kaisen_settings') as b:
        b.drop_column('web_allowed_domains')
        b.drop_column('web_access')
