"""app_backup_config: admin lock (admin_disabled, reason) and per-app storage ceiling override

Revision ID: c9d4f1a7b3e2
Revises: b4e8a2c6d1f7
Create Date: 2026-10-24 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c9d4f1a7b3e2'
down_revision: Union[str, None] = 'b4e8a2c6d1f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_backup_config') as b:
        b.add_column(sa.Column('admin_disabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('admin_disabled_reason', sa.String(length=255), nullable=True))
        b.add_column(sa.Column('max_bytes_override', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('app_backup_config') as b:
        b.drop_column('max_bytes_override')
        b.drop_column('admin_disabled_reason')
        b.drop_column('admin_disabled')
