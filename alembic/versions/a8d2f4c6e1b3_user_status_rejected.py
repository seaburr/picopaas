"""users.status 'rejected': convert signups an admin rejected under the old flow (soft-deleted, never approved)

users.status is a plain String(16) with no CHECK or enum, so no schema change is needed.

Signal: status 'deleted', role 'user', a `user.register` audit row with detail 'status=pending' (positive proof it
signed up pending; the first admin registers 'status=active'), and no `user.approve`/`user.promote`/`user.demote` row.
(Security review 2026-11-11: the first version only required "no approve row", which also matched a demoted former
admin or an account approved while audit writes failed; an admin Approve would have revived it.) Older text follows.
Original signal: no `user.approve` audit row for that user id. Anyone who was ever approved
(or suspended after approval, then deleted) has such a row, as does everyone approved through the admin UI. The first
platform admin is role 'platform_admin' and is excluded. Accepted imprecision: a signup an admin deleted while it was
still pending (the old Reject button was exactly that) is what we want to convert; a user approved straight in the
database (no audit row) would be misclassified, which only happens in manual ops.

The audit log is pruned after AUDIT_RETENTION_DAYS, so an absent approve row only proves "never approved" for users
created inside that window (their whole life is covered). Older deleted users are left alone: converting none of
them is safe, converting them on missing evidence is not.

Revision ID: a8d2f4c6e1b3
Revises: e6b1d3a8c470
Create Date: 2026-11-11 12:00:00.000000

"""
from typing import Sequence, Union

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision: str = 'a8d2f4c6e1b3'
down_revision: Union[str, None] = 'e6b1d3a8c470'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.config import AUDIT_RETENTION_DAYS
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=AUDIT_RETENTION_DAYS)
    op.get_bind().execute(sa.text(
        "UPDATE users SET status = 'rejected' WHERE status = 'deleted' AND role = 'user' "
        "AND created_at >= :cutoff "
        # positive proof it signed up as pending ...
        "AND EXISTS (SELECT 1 FROM audit_log r WHERE r.action = 'user.register' AND r.target_type = 'user' "
        "AND r.detail = 'status=pending' AND r.target_id = CAST(users.id AS CHAR(64))) "
        # ... and no trace of ever having been active (approve, or a demoted former admin). A deleted-after-active
        # account must never become 'rejected': an admin Approve would revive it.
        "AND NOT EXISTS (SELECT 1 FROM audit_log a WHERE a.action IN ('user.approve', 'user.promote', 'user.demote') "
        "AND a.target_type = 'user' AND a.target_id = CAST(users.id AS CHAR(64)))"
    ), {"cutoff": cutoff})


def downgrade() -> None:
    op.execute("UPDATE users SET status = 'deleted' WHERE status = 'rejected'")
