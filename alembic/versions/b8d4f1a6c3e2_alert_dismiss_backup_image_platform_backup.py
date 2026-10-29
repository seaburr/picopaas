"""app_events.dismissed_at, backup_sets.image/image_digest, platform_backup_runs

Revision ID: b8d4f1a6c3e2
Revises: a7c2e9f4d6b1
Create Date: 2026-10-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b8d4f1a6c3e2'
down_revision: Union[str, None] = 'a7c2e9f4d6b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_events') as batch:
        batch.add_column(sa.Column('dismissed_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('image_update_error_dismissed', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('backup_sets') as batch:
        batch.add_column(sa.Column('image', sa.String(length=512), nullable=True))
        batch.add_column(sa.Column('image_digest', sa.String(length=80), nullable=True))
    op.create_table(
        'platform_backup_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('trigger', sa.String(length=12), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('bytes_added', sa.BigInteger(), nullable=True),
        sa.Column('repo_bytes', sa.BigInteger(), nullable=True),
        sa.Column('snapshot_id', sa.String(length=64), nullable=True),
        sa.Column('error', sa.String(length=255), nullable=True),
        sa.Column('job_name', sa.String(length=63), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('platform_backup_runs')
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('image_update_error_dismissed')
    with op.batch_alter_table('backup_sets') as batch:
        batch.drop_column('image_digest')
        batch.drop_column('image')
    with op.batch_alter_table('app_events') as batch:
        batch.drop_column('dismissed_at')
