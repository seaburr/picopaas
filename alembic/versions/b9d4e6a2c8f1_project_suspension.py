"""project suspension: projects.status..., apps.suspended_via (replaces suspended_by_user), users.suspended_*

Revision ID: b9d4e6a2c8f1
Revises: a8c3e5f1b2d4
Create Date: 2026-10-31 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b9d4e6a2c8f1'
down_revision: Union[str, None] = 'a8c3e5f1b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('suspended_reason', sa.String(length=500), nullable=True))
    op.add_column('users', sa.Column('suspended_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('suspended_by', sa.Integer(), nullable=True))
    op.add_column('projects', sa.Column('status', sa.String(length=16), nullable=False, server_default='active'))
    op.add_column('projects', sa.Column('suspended_at', sa.DateTime(), nullable=True))
    op.add_column('projects', sa.Column('suspended_by', sa.Integer(), nullable=True))
    op.add_column('projects', sa.Column('suspended_reason', sa.String(length=500), nullable=True))
    op.add_column('projects', sa.Column('suspended_by_user', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('apps', sa.Column('suspended_via', sa.String(length=16), nullable=True))
    op.add_column('apps', sa.Column('suspended_at', sa.DateTime(), nullable=True))
    op.add_column('apps', sa.Column('suspended_by', sa.Integer(), nullable=True))
    op.add_column('apps', sa.Column('suspended_reason', sa.String(length=500), nullable=True))
    # suspended_by_user=true -> 'user'; any other suspended app was suspended on its own by an admin -> 'admin'.
    op.execute("UPDATE apps SET suspended_via = 'user' WHERE status = 'suspended' AND suspended_by_user = " +
               ("1" if op.get_bind().dialect.name == "sqlite" else "TRUE"))
    op.execute("UPDATE apps SET suspended_via = 'admin' WHERE status = 'suspended' AND suspended_via IS NULL")
    # A suspended account's personal project is suspended too (as it would be if suspended after this change).
    op.execute("UPDATE projects SET status = 'suspended', suspended_by_user = " +
               ("1" if op.get_bind().dialect.name == "sqlite" else "TRUE") +
               ", suspended_reason = 'Account suspended' WHERE is_personal = " +
               ("1" if op.get_bind().dialect.name == "sqlite" else "TRUE") +
               " AND deleted_at IS NULL AND created_by IN (SELECT id FROM users WHERE status = 'suspended')")
    op.drop_column('apps', 'suspended_by_user')


def downgrade() -> None:
    op.add_column('apps', sa.Column('suspended_by_user', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE apps SET suspended_by_user = " + ("1" if op.get_bind().dialect.name == "sqlite" else "TRUE") +
               " WHERE status = 'suspended' AND suspended_via = 'user'")
    for col in ('suspended_reason', 'suspended_by', 'suspended_at', 'suspended_via'):
        op.drop_column('apps', col)
    for col in ('suspended_by_user', 'suspended_reason', 'suspended_by', 'suspended_at', 'status'):
        op.drop_column('projects', col)
    for col in ('suspended_by', 'suspended_at', 'suspended_reason'):
        op.drop_column('users', col)
