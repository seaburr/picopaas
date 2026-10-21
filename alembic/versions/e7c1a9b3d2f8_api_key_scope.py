"""api_keys.scope (two-tier: read | full)

Revision ID: e7c1a9b3d2f8
Revises: d2f6a8c0e4b9
Create Date: 2026-10-20 18:30:00.000000

Adds a per-key access tier. server_default 'full' so every pre-existing key keeps the
full-access behavior it had before scopes existed (Chas, 2026-10-20, KISS two-tier).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e7c1a9b3d2f8'
down_revision: Union[str, None] = 'd2f6a8c0e4b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'api_keys',
        sa.Column('scope', sa.String(length=16), nullable=False, server_default='full'),
    )


def downgrade() -> None:
    op.drop_column('api_keys', 'scope')
