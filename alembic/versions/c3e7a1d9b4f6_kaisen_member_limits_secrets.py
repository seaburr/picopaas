"""kaisen per-member spend limits, build secrets, run/event attribution

Revision ID: c3e7a1d9b4f6
Revises: b5d8f1a3c6e2
Create Date: 2026-10-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3e7a1d9b4f6'
down_revision: Union[str, None] = 'b5d8f1a3c6e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kaisen_member_limits',
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('cap_day', sa.Float(), nullable=True),
        sa.Column('cap_build', sa.Float(), nullable=True),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'kaisen_secrets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('build_id', sa.Integer(), sa.ForeignKey('kaisen_builds.id'), nullable=False),
        sa.Column('name', sa.String(64), nullable=False),
        sa.Column('value_enc', sa.Text(), nullable=False),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('build_id', 'name', name='uq_kaisen_secret_name'),
    )
    op.create_index('ix_kaisen_secrets_build_id', 'kaisen_secrets', ['build_id'])
    with op.batch_alter_table('kaisen_runs') as b:
        b.add_column(sa.Column('started_by', sa.Integer(), nullable=True))
    with op.batch_alter_table('kaisen_events') as b:
        b.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        b.create_index('ix_kaisen_events_user_id', ['user_id'])


def downgrade() -> None:
    with op.batch_alter_table('kaisen_events') as b:
        b.drop_index('ix_kaisen_events_user_id')
        b.drop_column('user_id')
    with op.batch_alter_table('kaisen_runs') as b:
        b.drop_column('started_by')
    op.drop_index('ix_kaisen_secrets_build_id', table_name='kaisen_secrets')
    op.drop_table('kaisen_secrets')
    op.drop_table('kaisen_member_limits')
