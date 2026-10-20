"""Validation for per-app HTTP health check paths. Used by the API schemas and the YAML spec."""
import re

DEFAULT_HEALTH_PATH = "/healthz"
_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&()*+,;=:@/%-]{0,254}")  # no quote chars; fullmatch only (no `$` newline quirk)


def validate_health_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("Health check path can't be empty. Start it with /, for example /healthz.")
    if not _PATH_RE.fullmatch(path):
        raise ValueError(
            "Health check path must start with / and use only letters, digits and URL-safe characters (up to 255)."
        )
    for seg in path.split("/"):
        if seg.lower().replace("%2e", ".").strip() == ".." or seg == "..":
            raise ValueError("Health check path can't contain .. segments.")
    return path
