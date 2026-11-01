"""Validation for app env var keys and `${{ ref }}` reference values.

See docs/ARCHITECTURE.md "Reference variables": a user maps a platform-provided
reference like `${{ db.url }}` onto their own env var name. The allowed ref
paths mirror that section exactly.
"""
import re

from app import config

ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\Z")  # \Z, not $: "$" also matches before a trailing newline
REF_RE = re.compile(r"^\$\{\{\s*([a-z0-9_]+(?:\.[a-z0-9_]+)?)\s*\}\}$")

# One reference anywhere inside a value (no anchors). `resolve` and `validate_env_value` scan with this.
REF_ANY_RE = re.compile(r"\$\{\{\s*([a-z0-9_]+(?:\.[a-z0-9_]+)?)\s*\}\}")

ALLOWED_REFS = {
    "db.host", "db.port", "db.name", "db.user", "db.password", "db.url",
    "db.ca_cert", "db.ca_cert_body", "db.ca_file", "db.tls",
    "storage.endpoint", "storage.region", "storage.bucket",
    "storage.access_key", "storage.secret_key",
    "app.url",
}


class EnvValidationError(ValueError):
    pass


def db_url(engine: str, user: str, password: str, host: str, port: int | str, name: str, tls: bool = False) -> str:
    """Connection URL for `${{ db.url }}`: `postgres://` for engine 'pg' (with `?sslmode=require` on a TLS server),
    `mysql://` otherwise. Engine-neutral parts (host/port/name/user/password) are separate refs."""
    from urllib.parse import quote

    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{name}"
    if engine == "pg":
        return f"postgres://{auth}" + ("?sslmode=require" if tls else "")
    return f"mysql://{user}:{password}@{host}:{port}/{name}"


def pem_body(pem: str | None) -> str:
    """`${{ db.ca_cert_body }}`: the base64 body of the FIRST certificate in a PEM, on one line, without the
    BEGIN/END lines. For apps that take the CA in that form and add the PEM armour themselves (Wiki.js 2.x
    DB_SSL_CA). Empty string when there is no certificate."""
    m = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", pem or "", re.S)
    return "".join(m.group(1).split()) if m else ""


def validate_env_key(key: str) -> None:
    if not ENV_KEY_RE.match(key):
        raise EnvValidationError(
            f"{key!r} isn't a valid env var name. Use UPPER_SNAKE_CASE: capital letters, digits and "
            "underscores, not starting with a digit."
        )


def resolve_ref(value: str) -> str | None:
    """Return the dotted ref path (e.g. 'db.url') if value is a `${{ ... }}` reference."""
    match = REF_RE.match(value.strip())
    return match.group(1) if match else None


def resolve(env_vars: dict[str, str], refs: dict[str, str]) -> dict[str, str]:
    """Substitute every `${{ x.y }}` reference in each value with its real value.

    `env_vars` maps env var name -> raw value: a literal, a `${{ ref }}` placeholder, or literal text with any number of
    refs mixed in (see ALLOWED_REFS). `refs` maps a dotted ref path (e.g. "db.url") to its real value for this app
    (built in the reconciler from the provider creds, see app/providers/).

    Single pass over the ORIGINAL value: a resolved value is inserted as is (no encoding) and never re-scanned, so a
    resolved text that happens to contain `${{` is not expanded again. Used at deploy time, after `validate_env_value`
    has confirmed every ref is valid and available. Raises EnvValidationError if a ref has no entry in `refs`, e.g. the
    provider call that should have populated it hasn't run yet.
    """
    def _sub(m: re.Match) -> str:
        ref = m.group(1)
        if ref not in refs:
            raise EnvValidationError(f"unknown reference {m.group(0)!r}: no resolved value for {ref!r}")
        return refs[ref]

    resolved: dict[str, str] = {}
    for key, value in env_vars.items():
        whole = resolve_ref(value)
        if whole is not None:  # a whole-value reference (surrounding whitespace ignored), as before
            if whole not in refs:
                raise EnvValidationError(f"unknown reference {value!r}: no resolved value for {whole!r}")
            resolved[key] = refs[whole]
        else:
            resolved[key] = REF_ANY_RE.sub(_sub, value) if "${{" in value else value
    return resolved


def validate_env_value(value: str, want_db: bool, want_storage: bool) -> None:
    """Raise EnvValidationError unless every `${{` in value starts a valid, allowed, available reference.

    A value is literal text with any number of `${{ ref }}` references (whitespace inside the braces is fine, at most
    ENV_MAX_REFS_PER_VALUE of them). Look-alikes, wrong case, unknown refs, stray or unclosed `${{` are rejected to
    avoid confusion; write a literal `${{` is therefore not possible.
    """
    if "${{" not in value:
        return
    allowed = ", ".join("${{ %s }}" % r for r in sorted(ALLOWED_REFS))
    found = list(REF_ANY_RE.finditer(value))
    if len(found) != value.count("${{"):
        raise EnvValidationError(
            "A value containing '${{' may only contain complete references like ${{ db.url }} (lowercase, "
            f"closed with '}}}}') mixed with plain text. Allowed: {allowed}"
        )
    if len(found) > config.ENV_MAX_REFS_PER_VALUE:
        raise EnvValidationError(
            f"A value can contain at most {config.ENV_MAX_REFS_PER_VALUE} references, this one has {len(found)}.")
    for m in found:
        ref = m.group(1)
        if ref not in ALLOWED_REFS:
            raise EnvValidationError(f"Unknown reference ${{{{ {ref} }}}}. Allowed: {allowed}")
        domain = ref.split(".", 1)[0]
        if domain == "db" and not want_db:
            raise EnvValidationError(
                f"${{{{ {ref} }}}} needs a database, but this app doesn't have one. Turn on the database "
                "option for this app, or remove the reference.")
        if domain == "storage" and not want_storage:
            raise EnvValidationError(
                f"${{{{ {ref} }}}} needs a storage bucket, but this app doesn't have one. Turn on the storage bucket "
                "option for this app, or remove the reference.")
