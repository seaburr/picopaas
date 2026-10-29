"""projects, project_members, project_invites, apps.project_id (+ personal project backfill)

Revision ID: c9e5a2b7d4f1
Revises: b8d4f1a6c3e2
Create Date: 2026-10-28 15:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c9e5a2b7d4f1'
down_revision: Union[str, None] = 'b8d4f1a6c3e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'projects',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=80), nullable=False, unique=True),
        sa.Column('is_personal', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('app_limit', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_projects_created_by', 'projects', ['created_by'])
    op.create_table(
        'project_members',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('project_id', 'user_id', name='uq_project_member'),
    )
    op.create_index('ix_project_members_project_id', 'project_members', ['project_id'])
    op.create_index('ix_project_members_user_id', 'project_members', ['user_id'])
    op.create_table(
        'project_invites',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('token_hash', sa.String(length=64), nullable=False, unique=True),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('invited_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('accepted_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_project_invites_project_id', 'project_invites', ['project_id'])
    with op.batch_alter_table('apps') as batch:
        batch.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_apps_project_id', 'projects', ['project_id'], ['id'])
        batch.create_index('ix_apps_project_id', ['project_id'])

    # Backfill: one personal project (owner membership) per user, then point their apps at it.
    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    users = sa.table('users', sa.column('id', sa.Integer))
    projects = sa.table('projects', sa.column('id', sa.Integer), sa.column('name', sa.String), sa.column('slug', sa.String),
                        sa.column('is_personal', sa.Boolean), sa.column('created_by', sa.Integer), sa.column('created_at', sa.DateTime))
    members = sa.table('project_members', sa.column('project_id', sa.Integer), sa.column('user_id', sa.Integer),
                       sa.column('role', sa.String), sa.column('added_at', sa.DateTime))
    apps = sa.table('apps', sa.column('owner_id', sa.Integer), sa.column('project_id', sa.Integer))
    for (uid,) in bind.execute(sa.select(users.c.id)).fetchall():
        bind.execute(projects.insert().values(name='Personal', slug=f'personal-{uid}', is_personal=True,
                                              created_by=uid, created_at=now))
        pid = bind.execute(sa.select(projects.c.id).where(projects.c.slug == f'personal-{uid}')).scalar_one()
        bind.execute(members.insert().values(project_id=pid, user_id=uid, role='owner', added_at=now))
        bind.execute(apps.update().where(apps.c.owner_id == uid).values(project_id=pid))


def downgrade() -> None:
    with op.batch_alter_table('apps') as batch:
        batch.drop_index('ix_apps_project_id')
        batch.drop_constraint('fk_apps_project_id', type_='foreignkey')
        batch.drop_column('project_id')
    op.drop_table('project_invites')
    op.drop_table('project_members')
    op.drop_table('projects')
