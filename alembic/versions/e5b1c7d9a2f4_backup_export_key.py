"""backups: export object key for owner downloads (trigger == "export" runs)

Revision ID: e5b1c7d9a2f4
Revises: d4a9e1b7c3f2
Create Date: 2026-10-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e5b1c7d9a2f4'
down_revision: Union[str, None] = 'd4a9e1b7c3f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('backup_runs') as b:
        b.add_column(sa.Column('export_key', sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('backup_runs') as b:
        b.drop_column('export_key')
