"""kaisen file uploads: kaisen_assets, upload blocklist, projects.kaisen_uploads_enabled

Revision ID: b3e7d1a9c5f2
Revises: a9c4e1f7b2d6
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'b3e7d1a9c5f2'
down_revision: Union[str, None] = 'a9c4e1f7b2d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('projects') as b:
        b.add_column(sa.Column('kaisen_uploads_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        'kaisen_assets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('kaisen_builds.id'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('path', sa.String(120), nullable=False),
        sa.Column('name_orig', sa.String(100), nullable=False),
        sa.Column('mime', sa.String(32), nullable=False),
        sa.Column('ext', sa.String(8), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('orig_sha256', sa.String(64), nullable=False),
        sa.Column('width', sa.Integer(), nullable=True),
        sa.Column('height', sa.Integer(), nullable=True),
        sa.Column('description', sa.String(200), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('data', sa.LargeBinary().with_variant(mysql.LONGBLOB(), 'mysql'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('build_id', 'path', name='uq_kaisen_asset_path'),
    )
    op.create_index('ix_kaisen_assets_build_id', 'kaisen_assets', ['build_id'])
    op.create_index('ix_kaisen_assets_sha256', 'kaisen_assets', ['sha256'])
    op.create_index('ix_kaisen_assets_orig_sha256', 'kaisen_assets', ['orig_sha256'])
    op.create_table(
        'kaisen_upload_blocklist',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sha256', sa.String(64), nullable=False, unique=True),
        sa.Column('reason', sa.String(200), nullable=False),
        sa.Column('added_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('kaisen_upload_blocklist')
    op.drop_index('ix_kaisen_assets_orig_sha256', table_name='kaisen_assets')
    op.drop_index('ix_kaisen_assets_sha256', table_name='kaisen_assets')
    op.drop_index('ix_kaisen_assets_build_id', table_name='kaisen_assets')
    op.drop_table('kaisen_assets')
    with op.batch_alter_table('projects') as b:
        b.drop_column('kaisen_uploads_enabled')
