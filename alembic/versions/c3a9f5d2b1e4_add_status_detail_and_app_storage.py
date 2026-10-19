"""add status_detail to apps and app_storage table (reconciler)

Revision ID: c3a9f5d2b1e4
Revises: 8870a895c11e
Create Date: 2026-10-18 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3a9f5d2b1e4'
down_revision: Union[str, None] = '8870a895c11e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('apps', sa.Column('status_detail', sa.String(length=255), nullable=True))
    op.create_table('app_storage',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('app_id', sa.Integer(), nullable=False),
    sa.Column('bucket', sa.String(length=255), nullable=False),
    sa.Column('endpoint', sa.String(length=255), nullable=False),
    sa.Column('region', sa.String(length=64), nullable=False),
    sa.Column('access_key', sa.String(length=128), nullable=False),
    sa.Column('secret_key_encrypted', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('over_quota', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('app_id')
    )


def downgrade() -> None:
    op.drop_table('app_storage')
    op.drop_column('apps', 'status_detail')
