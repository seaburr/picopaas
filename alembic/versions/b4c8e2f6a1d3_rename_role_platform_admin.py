"""rename user role 'uberadmin' -> 'platform_admin' (data only; users.role is VARCHAR(16), no CHECK/enum)

Rolling-deploy note: until old pods are replaced they still test role == 'uberadmin', so the
platform admin briefly loses admin rights in old pods. Accepted.

Revision ID: b4c8e2f6a1d3
Revises: a3f7c1d9e5b2
"""
from alembic import op

revision = "b4c8e2f6a1d3"
down_revision = "a3f7c1d9e5b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE users SET role = 'platform_admin' WHERE role = 'uberadmin'")


def downgrade() -> None:
    op.execute("UPDATE users SET role = 'uberadmin' WHERE role = 'platform_admin'")
