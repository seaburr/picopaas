"""backups: backup_sets (one backup = one package of db/bucket/volume parts) and backup_runs.set_id

Existing runs are grouped into sets: same app, same trigger (scheduled|manual|final), created within 10 seconds of the
first run of the group. A run with no neighbour becomes a one-part set.

Revision ID: a7d3e9b1c5f2
Revises: e5b1c7d9a2f4
Create Date: 2026-10-24 18:00:00.000000

"""
from datetime import timedelta
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7d3e9b1c5f2'
down_revision: Union[str, None] = 'e5b1c7d9a2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WINDOW = timedelta(seconds=10)


def upgrade() -> None:
    op.create_table(
        'backup_sets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('app_id', sa.Integer(), nullable=False),
        sa.Column('trigger', sa.String(length=12), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('delete_status', sa.String(length=8), nullable=True),
        sa.ForeignKeyConstraint(['app_id'], ['apps.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_backup_sets_app_created', 'backup_sets', ['app_id', 'created_at', 'id'])
    with op.batch_alter_table('backup_runs') as b:
        b.add_column(sa.Column('set_id', sa.Integer(), nullable=True))
        b.create_foreign_key('fk_backup_runs_set_id', 'backup_sets', ['set_id'], ['id'], ondelete='SET NULL')
        b.create_index('ix_backup_runs_set_id', ['set_id'])

    bind = op.get_bind()
    runs = sa.table('backup_runs', sa.column('id', sa.Integer), sa.column('app_id', sa.Integer), sa.column('trigger', sa.String),
                    sa.column('created_at', sa.DateTime), sa.column('set_id', sa.Integer))
    sets = sa.Table('backup_sets', sa.MetaData(), sa.Column('id', sa.Integer, primary_key=True), sa.Column('app_id', sa.Integer),
                    sa.Column('trigger', sa.String), sa.Column('created_at', sa.DateTime), sa.Column('delete_status', sa.String))
    rows = bind.execute(
        sa.select(runs.c.id, runs.c.app_id, runs.c.trigger, runs.c.created_at)
        .where(runs.c.trigger.in_(('scheduled', 'manual', 'final')))
        .order_by(runs.c.app_id, runs.c.trigger, runs.c.created_at, runs.c.id)
    ).all()
    current = None  # (app_id, trigger, first created_at, set id)
    for rid, app_id, trigger, created in rows:
        if current is None or current[0] != app_id or current[1] != trigger or created - current[2] > WINDOW:
            res = bind.execute(sa.insert(sets).values(app_id=app_id, trigger=trigger, created_at=created))
            current = (app_id, trigger, created, res.inserted_primary_key[0])
        bind.execute(sa.update(runs).where(runs.c.id == rid).values(set_id=current[3]))


def downgrade() -> None:
    with op.batch_alter_table('backup_runs') as b:
        b.drop_index('ix_backup_runs_set_id')
        b.drop_constraint('fk_backup_runs_set_id', type_='foreignkey')
        b.drop_column('set_id')
    op.drop_index('ix_backup_sets_app_created', table_name='backup_sets')
    op.drop_table('backup_sets')
