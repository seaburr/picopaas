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
    "db.ca_cert", "db.tls",
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


def resolve(env_vars: dict[str, str], refs: dict[str, str]) -> dict[str, str]:
    """Substitute `${{ x.y }}`-style reference values with real values.

    `env_vars` is a mapping of env var name -> raw value, where a value is either
    a literal or a `${{ ref }}` placeholder (see ALLOWED_REFS). `refs` maps a
    resolved dotted ref path (e.g. "db.url") to its real value for this app
    (typically built from a provider's `create_db_user`/`create_bucket_and_key`
    creds — see app/providers/).

    Used at deploy time, after `validate_env_value` has already confirmed the
    ref is syntactically valid and available (want_db/want_storage). Raises
    EnvValidationError if a value looks like a ref but `refs` has no entry for
    it — e.g. the provider call that should have populated it hasn't run yet.
    """
    resolved: dict[str, str] = {}
    for key, value in env_vars.items():
        ref = resolve_ref(value)
        if ref is None:
            resolved[key] = value
            continue
        if ref not in refs:
            raise EnvValidationError(f"unknown reference {value!r}: no resolved value for {ref!r}")
        resolved[key] = refs[ref]
    return resolved


def validate_env_value(value: str, want_db: bool, want_storage: bool) -> None:
    """Raise EnvValidationError unless value is a plain literal or exactly one valid, available ref.

    Any value containing `${{` must be exactly one allowed reference (whitespace inside the
    braces is fine); look-alikes, wrong case, and mid-string use are rejected to avoid confusion.
    """
    if "${{" not in value:
        return
    ref = resolve_ref(value)
    allowed = ", ".join("${{ %s }}" % r for r in sorted(ALLOWED_REFS))
    if ref is None or ref not in ALLOWED_REFS:
        raise EnvValidationError(
            f"invalid reference in value: a value containing '${{{{' must be exactly one "
            f"reference. Allowed: {allowed}"
        )
    domain = ref.split(".", 1)[0]
    if domain == "db" and not want_db:
        raise EnvValidationError(
            f"${{{{ {ref} }}}} references a database, but this app did not request one (set want_db)")
    if domain == "storage" and not want_storage:
        raise EnvValidationError(
            f"${{{{ {ref} }}}} references storage, but this app did not request any (set want_storage)")
