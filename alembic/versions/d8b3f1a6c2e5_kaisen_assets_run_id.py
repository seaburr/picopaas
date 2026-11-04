"""kaisen_assets.run_id: the run (message) an upload was attached to

Revision ID: d8b3f1a6c2e5
Revises: c5a2e8d4f7b1
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd8b3f1a6c2e5'
down_revision: Union[str, None] = 'c5a2e8d4f7b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('kaisen_assets') as b:
        b.add_column(sa.Column('run_id', sa.Integer(), nullable=True))
    # existing uploads count as attached to their build's newest run (builds without runs stay unbound)
    op.execute('UPDATE kaisen_assets SET run_id = (SELECT MAX(id) FROM kaisen_runs WHERE kaisen_runs.build_id = kaisen_assets.build_id)')


def downgrade() -> None:
    with op.batch_alter_table('kaisen_assets') as b:
        b.drop_column('run_id')
