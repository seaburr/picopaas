"""apps platform_subdomain column

Revision ID: a5e9c1d7b3f2
Revises: f3b8d6c4a1e9
Create Date: 2026-10-21 10:00:00.000000

Admin-assigned fixed subdomain label under APP_DOMAIN (e.g. blog.<APP_DOMAIN>). Nullable; existing apps keep only
their generated hostname.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a5e9c1d7b3f2'
down_revision: Union[str, None] = 'f3b8d6c4a1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('apps', sa.Column('platform_subdomain', sa.String(length=63), nullable=True))
    # Unique INDEX (SQLite-compatible); NULLs are distinct so apps without one don't collide.
    op.create_index('uq_apps_platform_subdomain', 'apps', ['platform_subdomain'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_apps_platform_subdomain', table_name='apps')
    op.drop_column('apps', 'platform_subdomain')
