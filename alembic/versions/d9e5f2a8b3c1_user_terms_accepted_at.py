"""users.terms_accepted_at

Revision ID: d9e5f2a8b3c1
Revises: c8d4e1f7a2b9
Create Date: 2026-10-19 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd9e5f2a8b3c1'
down_revision: Union[str, None] = 'c8d4e1f7a2b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('terms_accepted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'terms_accepted_at')
