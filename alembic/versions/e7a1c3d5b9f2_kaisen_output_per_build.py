"""kaisen output is per build: move repo/branch/mode/push/open_pr from kaisen_settings to kaisen_builds

Revision ID: e7a1c3d5b9f2
Revises: d2f6b8a1c4e7
Create Date: 2026-10-29 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e7a1c3d5b9f2'
down_revision: Union[str, None] = 'd2f6b8a1c4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MOVED = ('output_mode', 'github_repo', 'github_branch', 'push_on_finish', 'open_pr')


def upgrade() -> None:
    with op.batch_alter_table('kaisen_builds') as batch:
        batch.add_column(sa.Column('output_mode', sa.String(length=16), server_default='download', nullable=False))
        batch.add_column(sa.Column('github_repo', sa.String(length=255), server_default='', nullable=False))
        batch.add_column(sa.Column('github_branch', sa.String(length=255), server_default='', nullable=False))
        batch.add_column(sa.Column('push_on_finish', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch.add_column(sa.Column('open_pr', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column('github_auth', sa.String(length=16), server_default='project', nullable=False))
        batch.add_column(sa.Column('github_token_enc', sa.Text(), nullable=True))
    # Existing project-level output settings become each of the project's builds' settings (they used the project token).
    op.execute(
        "UPDATE kaisen_builds SET "
        "output_mode = (SELECT s.output_mode FROM kaisen_settings s WHERE s.project_id = kaisen_builds.project_id), "
        "github_repo = (SELECT s.github_repo FROM kaisen_settings s WHERE s.project_id = kaisen_builds.project_id), "
        "github_branch = (SELECT s.github_branch FROM kaisen_settings s WHERE s.project_id = kaisen_builds.project_id), "
        "push_on_finish = (SELECT s.push_on_finish FROM kaisen_settings s WHERE s.project_id = kaisen_builds.project_id), "
        "open_pr = (SELECT s.open_pr FROM kaisen_settings s WHERE s.project_id = kaisen_builds.project_id) "
        "WHERE EXISTS (SELECT 1 FROM kaisen_settings s WHERE s.project_id = kaisen_builds.project_id)")
    with op.batch_alter_table('kaisen_settings') as batch:
        for c in _MOVED:
            batch.drop_column(c)


def downgrade() -> None:
    with op.batch_alter_table('kaisen_settings') as batch:
        batch.add_column(sa.Column('github_repo', sa.String(length=255), server_default='', nullable=False))
        batch.add_column(sa.Column('github_branch', sa.String(length=255), server_default='', nullable=False))
        batch.add_column(sa.Column('output_mode', sa.String(length=16), server_default='download', nullable=False))
        batch.add_column(sa.Column('push_on_finish', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch.add_column(sa.Column('open_pr', sa.Boolean(), server_default=sa.false(), nullable=False))
    # Lossy by nature (one project value vs many builds): take the project's newest live build's values.
    op.execute(
        "UPDATE kaisen_settings SET "
        + ", ".join(
            f"{c} = COALESCE((SELECT b.{c} FROM kaisen_builds b WHERE b.project_id = kaisen_settings.project_id "
            f"AND b.deleted_at IS NULL ORDER BY b.id DESC LIMIT 1), {c})" for c in _MOVED))
    with op.batch_alter_table('kaisen_builds') as batch:
        for c in _MOVED + ('github_auth', 'github_token_enc'):
            batch.drop_column(c)
