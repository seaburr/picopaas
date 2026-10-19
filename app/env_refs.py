"""Validation for app env var keys and `${{ ref }}` reference values.

See docs/ARCHITECTURE.md "Reference variables": a user maps a platform-provided
reference like `${{ db.url }}` onto their own env var name. The allowed ref
paths mirror that section exactly.
"""
import re

ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
REF_RE = re.compile(r"^\$\{\{\s*([a-z0-9_]+(?:\.[a-z0-9_]+)?)\s*\}\}$")

ALLOWED_REFS = {
    "db.host", "db.port", "db.name", "db.user", "db.password", "db.url",
    "storage.endpoint", "storage.region", "storage.bucket",
    "storage.access_key", "storage.secret_key",
}


class EnvValidationError(ValueError):
    pass


def validate_env_key(key: str) -> None:
    if not ENV_KEY_RE.match(key):
        raise EnvValidationError(
            f"invalid env key {key!r}: must match ^[A-Z_][A-Z0-9_]*$"
        )


def resolve_ref(value: str) -> str | None:
    """Return the dotted ref path (e.g. 'db.url') if value is a `${{ ... }}` reference."""
    match = REF_RE.match(value.strip())
    return match.group(1) if match else None


def validate_env_value(value: str, want_db: bool, want_storage: bool) -> None:
    """Raise EnvValidationError if value looks like a ref but is invalid or unavailable."""
    ref = resolve_ref(value)
    if ref is None:
        return
    if ref not in ALLOWED_REFS:
        raise EnvValidationError(f"unknown reference {value!r}; allowed: {sorted(ALLOWED_REFS)}")
    domain = ref.split(".", 1)[0]
    if domain == "db" and not want_db:
        raise EnvValidationError(f"{value!r} references a database, but this app did not request one")
    if domain == "storage" and not want_storage:
        raise EnvValidationError(f"{value!r} references storage, but this app did not request any")
