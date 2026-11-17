"""kaisen: confirm the plan before building (kaisen_builds.plan_first, kaisen_settings.plan_first, both default false)

Revision ID: c9e4b7a2d6f1
Revises: f1a7c3d9e2b5
Create Date: 2026-11-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c9e4b7a2d6f1'
down_revision: Union[str, None] = 'f1a7c3d9e2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('kaisen_builds') as b:
        b.add_column(sa.Column('plan_first', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('kaisen_settings') as b:
        b.add_column(sa.Column('plan_first', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('kaisen_settings') as b:
        b.drop_column('plan_first')
    with op.batch_alter_table('kaisen_builds') as b:
        b.drop_column('plan_first')
