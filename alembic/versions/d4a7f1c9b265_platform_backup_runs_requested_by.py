"""platform_backup_runs.requested_by: the admin who started a platform backup download (K108)

Revision ID: d4a7f1c9b265
Revises: b3c8e5a1d7f4
Create Date: 2026-11-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4a7f1c9b265'
down_revision: Union[str, None] = 'b3c8e5a1d7f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable on purpose: existing rows (backups, and exports made before this) have no owner, and an export without one can
    # be fetched by nobody.
    with op.batch_alter_table('platform_backup_runs') as b:
        b.add_column(sa.Column('requested_by', sa.Integer(), nullable=True))
        b.create_foreign_key('fk_platform_backup_runs_requested_by_users', 'users', ['requested_by'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('platform_backup_runs') as b:
        b.drop_constraint('fk_platform_backup_runs_requested_by_users', type_='foreignkey')
        b.drop_column('requested_by')
