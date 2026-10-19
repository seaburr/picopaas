"""user status and audit log

Revision ID: d4e1a7b9c2f6
Revises: c3a9f5d2b1e4
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4e1a7b9c2f6'
down_revision: Union[str, None] = 'c3a9f5d2b1e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows become 'active'.
    op.add_column('users', sa.Column('status', sa.String(length=16), nullable=False, server_default='active'))
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('actor_email', sa.String(length=255), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=True),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_log_at'), 'audit_log', ['at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_audit_log_at'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_column('users', 'status')
