"""security_alerts table (rule-based admin alerts, app/security_alerts.py)

Revision ID: a7d3e1b9c4f2
Revises: c9e4b7a2d6f1
Create Date: 2026-11-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7d3e1b9c4f2'
down_revision: Union[str, None] = 'c9e4b7a2d6f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'security_alerts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(48), nullable=False),
        sa.Column('severity', sa.String(8), nullable=False),
        sa.Column('subject_type', sa.String(16), nullable=True),
        sa.Column('subject_id', sa.String(64), nullable=True),
        sa.Column('message', sa.String(255), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('dedupe_key', sa.String(160), nullable=False),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.Column('acknowledged_by', sa.Integer(), nullable=True),
        sa.UniqueConstraint('dedupe_key', name='uq_security_alerts_dedupe_key'),
    )
    op.create_index('ix_security_alerts_last_seen', 'security_alerts', ['last_seen'])


def downgrade() -> None:
    op.drop_index('ix_security_alerts_last_seen', table_name='security_alerts')
    op.drop_table('security_alerts')
