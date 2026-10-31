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
