"""apps custom_domain columns

Revision ID: f3b8d6c4a1e9
Revises: e7c1a9b3d2f8
Create Date: 2026-10-20 19:15:00.000000

User-attached custom domains + per-host auto-TLS. All nullable, so existing apps keep only their
generated *.APP_DOMAIN subdomain (custom_domain NULL).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f3b8d6c4a1e9'
down_revision: Union[str, None] = 'e7c1a9b3d2f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('apps', sa.Column('custom_domain', sa.String(length=255), nullable=True))
    op.add_column('apps', sa.Column('custom_domain_status', sa.String(length=16), nullable=True))
    op.add_column('apps', sa.Column('custom_domain_token', sa.String(length=64), nullable=True))
    op.add_column('apps', sa.Column('custom_domain_error', sa.String(length=255), nullable=True))
    op.add_column('apps', sa.Column('custom_domain_verified_at', sa.DateTime(), nullable=True))
    # Unique INDEX (not an ALTER-ADD-CONSTRAINT) so it applies on SQLite too; NULLs are distinct in both SQLite and
    # MySQL, so apps without a custom domain don't collide. Enforces one app per custom host.
    op.create_index('uq_apps_custom_domain', 'apps', ['custom_domain'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_apps_custom_domain', table_name='apps')
    op.drop_column('apps', 'custom_domain_verified_at')
    op.drop_column('apps', 'custom_domain_error')
    op.drop_column('apps', 'custom_domain_token')
    op.drop_column('apps', 'custom_domain_status')
    op.drop_column('apps', 'custom_domain')
