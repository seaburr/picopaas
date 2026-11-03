"""kaisen run hang watchdog: progress stamp, hang retries, attempt fence, error reason

Revision ID: a9c4e1f7b2d6
Revises: d7a2f4c8e1b3
Create Date: 2026-11-02 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a9c4e1f7b2d6'
down_revision: Union[str, None] = 'd7a2f4c8e1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('kaisen_runs') as b:
        b.add_column(sa.Column('last_progress_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('hang_retries', sa.Integer(), nullable=False, server_default='0'))
        b.add_column(sa.Column('attempt', sa.Integer(), nullable=False, server_default='0'))
        b.add_column(sa.Column('error_reason', sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('kaisen_runs') as b:
        b.drop_column('error_reason')
        b.drop_column('attempt')
        b.drop_column('hang_retries')
        b.drop_column('last_progress_at')
