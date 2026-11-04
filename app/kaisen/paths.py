"""Path rules for files the builder writes (same as standalone Kaisen)."""
import posixpath


class ToolError(Exception):
    """A tool call failed in a way the model can read and react to."""


def normalize_path(p) -> str:
    if not isinstance(p, str) or not p.strip():
        raise ToolError("path must be a non-empty string")
    if "\x00" in p or "\\" in p or len(p) > 200:
        raise ToolError("invalid path")
    if p.startswith("/"):
        raise ToolError("absolute paths are not allowed: use a path relative to the project root, "
                        'for example "app.py" instead of "/app/app.py"')
    if any(seg == ".." for seg in p.split("/")):
        raise ToolError("'..' is not allowed in paths")
    n = posixpath.normpath(p)
    if n in (".", "") or n.startswith("../"):
        raise ToolError("invalid path")
    if any(seg.lower() in (".git", ".github") for seg in n.split("/")):
        raise ToolError(".git/ and .github/ are not writable")
    return n


RESERVED_UPLOAD_ROOTS = ("uploads", "references")


def is_reserved_upload_path(p: str) -> bool:
    """True for anything under the upload roots (`uploads/`, `references/`, compared case-insensitively on the first
    segment, so `Uploads/x.png` cannot sneak past) and for the bundle's upload manifest. The model may not write there."""
    n = posixpath.normpath(p.replace("\\", "/")) if isinstance(p, str) else ""
    first = n.split("/")[0].lower()
    return first in RESERVED_UPLOAD_ROOTS or n.lower() == ".kaisen-uploads.json"


# ---- self-harm guards (docs/design/kaisen.md 16.10). The runtime (images/kaisen-runtime/boot.py) refuses to serve these
# names; the builder refuses to write them, so a secret never lands in a build. tests/test_kaisen_secret_guards.py keeps
# the two lists in step.
PRIVATE_KEY_EXT = ("pem", "key", "p12", "pfx", "jks", "keystore", "ppk", "gpg", "kdbx")
PRIVATE_KEY_PREFIX = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
SERVED_BLOCK_EXT = ("env", "tfstate", "tfvars", "sqlite", "sqlite3", "db", "sql", "dump", "bak", "swp", "swo", "orig", "old",
                    "log", "php", "py", "pyc", "rb", "pl", "sh", "cgi", "asp", "aspx", "jsp")
SERVED_BLOCK_PREFIX = ("wp-config.php", "secrets.", "secret.", "credentials", "docker-compose", "service-account")
SERVED_BLOCK_NAMES = ("kaisen.json", "dockerfile", "thumbs.db", "node_modules", "__pycache__", "composer.json", "composer.lock")


def is_secret_file(path: str) -> bool:
    """`.env*` and private-key / keystore files, in any folder. These never belong in a build (use Build secrets)."""
    for seg in path.lower().split("/"):
        if seg.startswith(".env") or seg.endswith(".env") or seg.startswith(PRIVATE_KEY_PREFIX):
            return True
        if "." in seg and seg.rsplit(".", 1)[-1] in PRIVATE_KEY_EXT:
            return True
    return False


def is_unservable_public(path: str) -> bool:
    """A name the preview runtime would 404 even inside public/ (dotfiles, backups, databases, logs, server source ...)."""
    for seg in path.lower().split("/"):
        if seg.startswith(".") or seg.endswith("~") or seg in SERVED_BLOCK_NAMES or seg.startswith(SERVED_BLOCK_PREFIX):
            return True
        if "." in seg and seg.rsplit(".", 1)[-1] in SERVED_BLOCK_EXT:
            return True
    return False


def check_write_name(path: str) -> None:
    """Raise ToolError when the builder must not create this file. `path` is already normalized."""
    if is_secret_file(path):
        raise ToolError("refused: .env files, private keys and keystores must not be written into a project. "
                        "Read credentials from environment variables, ask the human to add them under Build secrets "
                        "(ask_human), and document the variable names in README.md.")
    if path.lower().startswith("public/") and is_unservable_public(path):
        raise ToolError("refused: files in public/ are served to everyone, and this name (a dotfile, backup, database, log, "
                        "config or server source file) is never served. Keep it out of public/.")
