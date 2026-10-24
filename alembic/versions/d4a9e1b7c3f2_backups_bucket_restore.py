"""backups: bucket kind columns and restore source run (REQ-9 bucket + restore helpers)

Revision ID: d4a9e1b7c3f2
Revises: c2e8a4f6b1d7
Create Date: 2026-10-23 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4a9e1b7c3f2'
down_revision: Union[str, None] = 'c2e8a4f6b1d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_backup_config') as b:
        b.add_column(sa.Column('bucket_last_backup_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('bucket_last_status', sa.String(length=16), nullable=True))
        b.add_column(sa.Column('bucket_last_error', sa.String(length=255), nullable=True))
        b.add_column(sa.Column('bucket_last_bytes', sa.BigInteger(), nullable=True))
        b.add_column(sa.Column('bucket_repo_bytes', sa.BigInteger(), nullable=False, server_default='0'))
    with op.batch_alter_table('backup_runs') as b:
        b.add_column(sa.Column('source_run_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('backup_runs') as b:
        b.drop_column('source_run_id')
    with op.batch_alter_table('app_backup_config') as b:
        for c in ('bucket_repo_bytes', 'bucket_last_bytes', 'bucket_last_error', 'bucket_last_status', 'bucket_last_backup_at'):
            b.drop_column(c)
