"""Break-glass admin CLI, run inside the API pod: `kubectl exec -it deploy/<api> -- python -m app.manage <cmd>`.

Uses the app's DATABASE_URL/config. Every change writes an audit entry with actor "manage-cli".
  grant-admin <email>     make an existing user a platform admin and activate them
  list-admins             list platform admins (email, status)
  reset-password <email>  set and print a temporary password (user must change it at next login)
"""
import argparse
import secrets
import sys
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app import audit, models
from app.security import hash_password

ACTOR = "manage-cli"


def _find(db: Session, email: str) -> Optional[models.User]:
    return (db.query(models.User)
            .filter(models.User.email == email.strip().lower(), models.User.status != "deleted").first())


def grant_admin(db: Session, email: str) -> str:
    user = _find(db, email)
    if user is None:
        raise LookupError(f"No such user: {email}")
    was = (user.role, user.status)
    user.role = "platform_admin"
    user.status = "active"
    db.commit()
    audit.record(db, None, "user.promote", "user", user.id, f"was role={was[0]} status={was[1]}", actor_email=ACTOR)
    return f"{user.email} is now an active platform admin."


def list_admins(db: Session) -> list[str]:
    rows = db.query(models.User).filter(models.User.role == "platform_admin").order_by(models.User.id).all()
    return [f"{u.id}\t{u.email}\t{u.status}" for u in rows]


def reset_password(db: Session, email: str) -> str:
    user = _find(db, email)
    if user is None:
        raise LookupError(f"No such user: {email}")
    temp = secrets.token_urlsafe(12)
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    db.commit()
    audit.record(db, None, "user.reset_password", "user", user.id, actor_email=ACTOR)
    return temp


def main(argv: Optional[Sequence[str]] = None, session_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("grant-admin").add_argument("email")
    sub.add_parser("list-admins")
    sub.add_parser("reset-password").add_argument("email")
    args = parser.parse_args(argv)
    if session_factory is None:
        from app.db import SessionLocal as session_factory
    db = session_factory()
    try:
        if args.cmd == "grant-admin":
            print(grant_admin(db, args.email))
        elif args.cmd == "list-admins":
            print("id\temail\tstatus")
            for line in list_admins(db):
                print(line)
        else:
            print(f"Temporary password for {args.email.strip().lower()} (shown once; they must change it at login):")
            print(reset_password(db, args.email))
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
