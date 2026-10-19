"""apps.cpu_request_m / cpu_limit_m / mem_request_mi / mem_limit_mi (uberadmin resource overrides)

Revision ID: c8d4e1f7a2b9
Revises: b7e2d9a4c1f3
Create Date: 2026-10-19 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c8d4e1f7a2b9'
down_revision: Union[str, None] = 'b7e2d9a4c1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLS = ('cpu_request_m', 'cpu_limit_m', 'mem_request_mi', 'mem_limit_mi')


def upgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        for c in COLS:
            batch.add_column(sa.Column(c, sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        for c in reversed(COLS):
            batch.drop_column(c)
