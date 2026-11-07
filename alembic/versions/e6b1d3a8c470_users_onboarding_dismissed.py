"""users.onboarding_dismissed: per-user flag, getting-started dialog dismissed

Revision ID: e6b1d3a8c470
Revises: d4a7f1c9b265
Create Date: 2026-11-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e6b1d3a8c470'
down_revision: Union[str, None] = 'd4a7f1c9b265'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('onboarding_dismissed', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.drop_column('onboarding_dismissed')
