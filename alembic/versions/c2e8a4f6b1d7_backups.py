"""backups: app_backup_config and backup_runs (REQ-9 phase 1a)

Revision ID: c2e8a4f6b1d7
Revises: b6d3f1a8c2e4
Create Date: 2026-10-23 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c2e8a4f6b1d7'
down_revision: Union[str, None] = 'b6d3f1a8c2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_backup_config',
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('apps.id', ondelete='CASCADE'), primary_key=True, autoincrement=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('schedule', sa.String(length=8), nullable=False, server_default='daily'),
        sa.Column('repo_password_enc', sa.Text(), nullable=True),
        sa.Column('next_backup_at', sa.DateTime(), nullable=True),
        sa.Column('last_backup_at', sa.DateTime(), nullable=True),
        sa.Column('last_success_at', sa.DateTime(), nullable=True),
        sa.Column('last_status', sa.String(length=16), nullable=True),
        sa.Column('last_error', sa.String(length=255), nullable=True),
        sa.Column('db_last_backup_at', sa.DateTime(), nullable=True),
        sa.Column('db_last_status', sa.String(length=16), nullable=True),
        sa.Column('db_last_error', sa.String(length=255), nullable=True),
        sa.Column('db_last_bytes', sa.BigInteger(), nullable=True),
        sa.Column('db_repo_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('vol_last_backup_at', sa.DateTime(), nullable=True),
        sa.Column('vol_last_status', sa.String(length=16), nullable=True),
        sa.Column('vol_last_error', sa.String(length=255), nullable=True),
        sa.Column('vol_last_bytes', sa.BigInteger(), nullable=True),
        sa.Column('vol_repo_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('deleted_retention_until', sa.DateTime(), nullable=True),
        sa.Column('purged_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_app_backup_config_next_backup_at', 'app_backup_config', ['next_backup_at'])
    op.create_table(
        'backup_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('apps.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
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
    op.create_index('ix_backup_runs_app_created', 'backup_runs', ['app_id', 'created_at', 'id'])
    op.create_index('ix_backup_runs_status', 'backup_runs', ['status'])


def downgrade() -> None:
    op.drop_index('ix_backup_runs_status', table_name='backup_runs')
    op.drop_index('ix_backup_runs_app_created', table_name='backup_runs')
    op.drop_table('backup_runs')
    op.drop_index('ix_app_backup_config_next_backup_at', table_name='app_backup_config')
    op.drop_table('app_backup_config')
