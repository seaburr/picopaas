"""apps.owner_* resource choices (owner sliders below the admin ceilings) and users.theme

Revision ID: c5e8a2d7b941
Revises: a7f2c9d41b36
Create Date: 2026-11-05 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c5e8a2d7b941'
down_revision: Union[str, None] = 'a7f2c9d41b36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OWNER_COLS = ('owner_cpu_limit_m', 'owner_mem_limit_mi', 'owner_ephemeral_limit_mi', 'owner_storage_quota_mb')


def upgrade() -> None:
    with op.batch_alter_table('apps') as b:
        for c in _OWNER_COLS:  # NULL = use the full ceiling, so existing apps are unchanged
            b.add_column(sa.Column(c, sa.Integer(), nullable=True))
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('theme', sa.String(length=8), nullable=False, server_default='dark'))


def downgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.drop_column('theme')
    with op.batch_alter_table('apps') as b:
        for c in reversed(_OWNER_COLS):
            b.drop_column(c)
