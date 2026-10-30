"""kaisen_preview_logs: latest redacted log tail of a preview app, written by the reconciler for deploy_preview

Revision ID: a9c4e2b7d1f3
Revises: e7a1c3d5b9f2
Create Date: 2026-10-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'a9c4e2b7d1f3'
down_revision: Union[str, None] = 'e7a1c3d5b9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kaisen_preview_logs',
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('apps.id'), primary_key=True),
        sa.Column('text', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql'), nullable=False),
        sa.Column('captured_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('kaisen_preview_logs')
