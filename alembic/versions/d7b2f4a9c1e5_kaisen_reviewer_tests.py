"""kaisen reviewer settings, run review verdict, build test result

Revision ID: d7b2f4a9c1e5
Revises: c3e7a1d9b4f6
Create Date: 2026-10-30 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd7b2f4a9c1e5'
down_revision: Union[str, None] = 'c3e7a1d9b4f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('kaisen_settings') as b:
        b.add_column(sa.Column('reviewer', sa.String(8), nullable=False, server_default='off'))
        b.add_column(sa.Column('reviewer_model', sa.String(64), nullable=True))
    with op.batch_alter_table('kaisen_runs') as b:
        b.add_column(sa.Column('review', sa.JSON(), nullable=True))
    with op.batch_alter_table('kaisen_builds') as b:
        b.add_column(sa.Column('test_status', sa.String(16), nullable=True))
        b.add_column(sa.Column('test_ran_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('test_tail', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('kaisen_builds') as b:
        b.drop_column('test_tail')
        b.drop_column('test_ran_at')
        b.drop_column('test_status')
    with op.batch_alter_table('kaisen_runs') as b:
        b.drop_column('review')
    with op.batch_alter_table('kaisen_settings') as b:
        b.drop_column('reviewer_model')
        b.drop_column('reviewer')
