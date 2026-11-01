"""db_servers.flavor: which DbFlavor (managed-DB quirks) a server uses

Revision ID: d7a2f4c8e1b3
Revises: c5e7f9a1b3d2
Create Date: 2026-11-01

Existing rows get 'do-managed' (the server_default), i.e. exactly today's behaviour.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd7a2f4c8e1b3'
down_revision: Union[str, None] = 'c5e7f9a1b3d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("db_servers") as b:
        b.add_column(sa.Column("flavor", sa.String(length=16), nullable=False, server_default="do-managed"))


def downgrade() -> None:
    with op.batch_alter_table("db_servers") as b:
        b.drop_column("flavor")
