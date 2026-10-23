"""scaling indexes (reconciler/list hot paths), FK indexes, app_databases.db_conn_limit_applied

Revision ID: a7c1e9b3d5f2
Revises: e8b2c5d7a914
Create Date: 2026-10-23 12:00:00.000000

Composite indexes are created before the single-column ones they replace are dropped, so MySQL always has an
index usable by the foreign key.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7c1e9b3d5f2'
down_revision: Union[str, None] = 'e8b2c5d7a914'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('app_databases') as batch:
        batch.add_column(sa.Column('db_conn_limit_applied', sa.Integer(), nullable=True))

    op.create_index('ix_apps_deleted_status', 'apps', ['deleted_at', 'status'])
    op.create_index('ix_apps_owner_deleted', 'apps', ['owner_id', 'deleted_at'])
    op.create_index('ix_users_status_created', 'users', ['status', 'created_at'])
    op.create_index('ix_api_keys_user_id', 'api_keys', ['user_id'])
    op.create_index('ix_app_env_vars_app_id', 'app_env_vars', ['app_id'])
    op.create_index('ix_app_databases_db_server_id', 'app_databases', ['db_server_id'])
    op.create_index('ix_app_events_app_at', 'app_events', ['app_id', 'at', 'id'])
    op.create_index('ix_app_metric_samples_app_ts', 'app_metric_samples', ['app_id', 'ts'])
    op.drop_index('ix_app_events_app_id', table_name='app_events')
    op.drop_index('ix_app_metric_samples_app_id', table_name='app_metric_samples')


def downgrade() -> None:
    op.create_index('ix_app_metric_samples_app_id', 'app_metric_samples', ['app_id'])
    op.create_index('ix_app_events_app_id', 'app_events', ['app_id'])
    op.drop_index('ix_app_metric_samples_app_ts', table_name='app_metric_samples')
    op.drop_index('ix_app_events_app_at', table_name='app_events')
    op.drop_index('ix_app_databases_db_server_id', table_name='app_databases')
    op.drop_index('ix_app_env_vars_app_id', table_name='app_env_vars')
    op.drop_index('ix_api_keys_user_id', table_name='api_keys')
    op.drop_index('ix_users_status_created', table_name='users')
    op.drop_index('ix_apps_owner_deleted', table_name='apps')
    op.drop_index('ix_apps_deleted_status', table_name='apps')
    with op.batch_alter_table('app_databases') as batch:
        batch.drop_column('db_conn_limit_applied')
