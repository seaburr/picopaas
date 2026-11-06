"""kaisen_settings.package_steward: per-project switch for the package steward pass (default on)

Revision ID: b3c8e5a1d7f4
Revises: c5e8a2d7b941
Create Date: 2026-11-05 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b3c8e5a1d7f4'
down_revision: Union[str, None] = 'c5e8a2d7b941'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('kaisen_settings') as b:
        b.add_column(sa.Column('package_steward', sa.String(8), nullable=False, server_default='on'))


def downgrade() -> None:
    with op.batch_alter_table('kaisen_settings') as b:
        b.drop_column('package_steward')
