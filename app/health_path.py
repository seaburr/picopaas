"""Validation for per-app HTTP health check paths. Used by the API schemas and the YAML spec."""
import re

DEFAULT_HEALTH_PATH = "/healthz"
_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&()*+,;=:@/%-]{0,254}")  # no quote chars; fullmatch only (no `$` newline quirk)


def validate_health_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("health check path must not be empty")
    if not _PATH_RE.fullmatch(path):
        raise ValueError(
            "health check path must start with '/' and contain only letters, digits and ._~!$&()*+,;=:@/%- (max 255 chars)"
        )
    for seg in path.split("/"):
        if seg.lower().replace("%2e", ".").strip() == ".." or seg == "..":
            raise ValueError("health check path must not contain '..' segments")
    return path
