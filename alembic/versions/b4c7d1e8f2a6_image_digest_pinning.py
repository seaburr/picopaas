"""apps.image_digest/auto_update/image_checked_at/image_update_error (digest pinning + floating-tag watcher)

Existing rows get image_digest NULL (rendered as the plain image until the watcher fills it in).

Revision ID: b4c7d1e8f2a6
Revises: f1a8c4d7e2b5
Create Date: 2026-10-19 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b4c7d1e8f2a6'
down_revision: Union[str, None] = 'f1a8c4d7e2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('image_digest', sa.String(length=80), nullable=True))
        batch.add_column(sa.Column('auto_update', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column('image_checked_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('image_update_error', sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_column('image_update_error')
        batch.drop_column('image_checked_at')
        batch.drop_column('auto_update')
        batch.drop_column('image_digest')
