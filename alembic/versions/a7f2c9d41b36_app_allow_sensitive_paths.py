"""apps.allow_sensitive_paths: opt out of the edge block on /.env and similar paths

Revision ID: a7f2c9d41b36
Revises: d8b3f1a6c2e5
Create Date: 2026-11-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7f2c9d41b36'
down_revision: Union[str, None] = 'd8b3f1a6c2e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('apps') as b:
        b.add_column(sa.Column('allow_sensitive_paths', sa.Boolean(), nullable=False, server_default=sa.false()))
    # Git hosts show repository files such as /user/repo/src/branch/main/.env, so existing ones keep working.
    op.execute(sa.text("UPDATE apps SET allow_sensitive_paths = :on WHERE LOWER(image) LIKE '%forgejo%' OR LOWER(image) LIKE '%gitea%'")
               .bindparams(on=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as b:
        b.drop_column('allow_sensitive_paths')
