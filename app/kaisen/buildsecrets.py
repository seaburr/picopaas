"""Build-level secrets: members set name/value pairs; they are Fernet-encrypted at rest, merged into the preview app's
env on every deploy (deploy.py), redacted from transcripts and logs, and never shown to the model (names only)."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, crypto
from app.env_refs import ENV_KEY_RE
from .models import KaisenSecret


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_name(name: str) -> None:
    if len(name) > config.KAISEN_SECRET_NAME_MAX or not ENV_KEY_RE.match(name):
        raise ValueError(f"{name[:40]!r} isn't a valid name. Use UPPER_SNAKE_CASE (capital letters, digits, underscores, "
                         f"not starting with a digit), at most {config.KAISEN_SECRET_NAME_MAX} characters.")
    if name.startswith(config.KAISEN_RESERVED_ENV_PREFIXES) or name in config.KAISEN_RESERVED_ENV_NAMES:
        raise ValueError(f"{name} is set by the platform for previews (names starting with "
                         f"{', '.join(config.KAISEN_RESERVED_ENV_PREFIXES)} and a few others are reserved). Pick another name.")


def validate_value(value: str) -> None:
    if len(value) < config.KAISEN_SECRET_VALUE_MIN:
        raise ValueError(f"A secret value needs at least {config.KAISEN_SECRET_VALUE_MIN} characters, so it can be "
                         "hidden reliably in logs. For short settings, use a longer value or put it in the code.")
    if len(value) > config.KAISEN_SECRET_VALUE_MAX:
        raise ValueError(f"A secret value can be at most {config.KAISEN_SECRET_VALUE_MAX} characters.")
    if "${{" in value:
        raise ValueError("A secret value can't contain '${{'.")


def rows(s: Session, build_id: int) -> list[KaisenSecret]:
    return list(s.scalars(select(KaisenSecret).where(KaisenSecret.build_id == build_id).order_by(KaisenSecret.name)))


def names(s: Session, build_id: int) -> list[str]:
    return [r.name for r in rows(s, build_id)]


def values(s: Session, build_id: int) -> dict[str, str]:
    out = {}
    for r in rows(s, build_id):
        v = crypto.try_decrypt(r.value_enc)
        if v:
            out[r.name] = v
    return out


def put(s: Session, build_id: int, name: str, value: str, by: Optional[int]) -> bool:
    """Create or replace. Returns True if it was new. Raises ValueError. No commit."""
    validate_name(name)
    validate_value(value)
    row = s.scalar(select(KaisenSecret).where(KaisenSecret.build_id == build_id, KaisenSecret.name == name))
    if row is None:
        if len(rows(s, build_id)) >= config.KAISEN_SECRETS_MAX:
            raise ValueError(f"A build can have at most {config.KAISEN_SECRETS_MAX} secrets. Delete one first.")
        s.add(KaisenSecret(build_id=build_id, name=name, value_enc=crypto.encrypt(value), updated_by=by, updated_at=_now()))
        return True
    row.value_enc, row.updated_by, row.updated_at = crypto.encrypt(value), by, _now()
    return False


def describe(s: Session, build_id: int) -> list[dict]:
    from app.schemas import MASKED_VALUE
    return [{"name": r.name, "value": MASKED_VALUE, "updated_at": r.updated_at.isoformat()} for r in rows(s, build_id)]


def prompt_note(s: Session, build_id: int) -> str:
    """Text for the model: names only."""
    ns = names(s, build_id)
    if not ns:
        return ""
    return ("\n\nSecrets the human has set for this app (names only; the values are hidden from you). They are available "
            "to the running app as environment variables with exactly these names; read them from the environment and "
            "never write a value into a file: " + ", ".join(ns) + ".")
