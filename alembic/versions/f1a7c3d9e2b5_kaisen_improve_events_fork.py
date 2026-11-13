"""kaisen: Improve brief spend as events (run_id/build_id nullable, kind), build fork provenance

kaisen_events.run_id and .build_id become nullable (an Improve brief press has no run and no build until the build is
created; the FK on run_id stays). kaisen_events.kind marks them ("improve"; NULL for normal run events).
kaisen_builds gets forked_from_id / forked_from_snapshot_id (plain integers, no FK: the source may be deleted later).

Revision ID: f1a7c3d9e2b5
Revises: a8d2f4c6e1b3
Create Date: 2026-11-12 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f1a7c3d9e2b5'
down_revision: Union[str, None] = 'a8d2f4c6e1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('kaisen_events') as b:
        b.alter_column('run_id', existing_type=sa.Integer(), nullable=True)
        b.alter_column('build_id', existing_type=sa.Integer(), nullable=True)
        b.add_column(sa.Column('kind', sa.String(length=16), nullable=True))
    with op.batch_alter_table('kaisen_builds') as b:
        b.add_column(sa.Column('forked_from_id', sa.Integer(), nullable=True))
        b.add_column(sa.Column('forked_from_snapshot_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('kaisen_builds') as b:
        b.drop_column('forked_from_snapshot_id')
        b.drop_column('forked_from_id')
    # events without a run or build cannot satisfy NOT NULL: drop them (their cost was only ever in the audit log too)
    op.execute("DELETE FROM kaisen_events WHERE run_id IS NULL OR build_id IS NULL")
    with op.batch_alter_table('kaisen_events') as b:
        b.drop_column('kind')
        b.alter_column('build_id', existing_type=sa.Integer(), nullable=False)
        b.alter_column('run_id', existing_type=sa.Integer(), nullable=False)
