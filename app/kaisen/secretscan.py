"""High-confidence credential detection shared by the builder tools and the upload route (docs/design/kaisen.md 16.10).
Returns the kind and the line number, never the value. A heuristic: custom secrets slip through, so it favours
precision (few false alarms on ordinary code) over recall."""
import base64
import re

_PATTERNS = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")),
    ("an OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("a PicoPaaS API key", re.compile(r"pp_sk_[A-Za-z0-9]{10,}")),
    ("a GitHub token", re.compile(r"(?:ghp_|gho_|ghs_|ghu_|ghr_|github_pat_)[A-Za-z0-9_]{20,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a Stripe key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a DigitalOcean token", re.compile(r"\bdo[opr]_v1_[a-f0-9]{40,}")),
    ("a JSON web token", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
]
_ASSIGN = re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key)[A-Za-z0-9_]{0,16}[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_+/=.~-]{16,})")
_ASSIGN_QUOTED = re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key)[A-Za-z0-9_]{0,16}[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_+/=.~-]{16,})[\"']")
_PLACEHOLDER = re.compile(r"(?i)example|your|fake|dummy|placeholder|changeme|change-me|not-a|test|sample|xxxx|\*\*\*|todo|env")
_DOC_PLACEHOLDER = re.compile(r"(?i)example|your|placeholder|changeme|change-me|xxxx|\*\*\*|process\.env|environ|getenv")
_DB_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@\"']{1,64}:(?!\$|\{|<|%s|\*|x{4})([^\s/@\"'<>{}$]{8,64})@(?!$)")
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")


def _line_kind(line: str, code: bool) -> str | None:
    for kind, rx in _PATTERNS:
        m = rx.search(line)
        if m and "EXAMPLE" not in m.group(0):
            return kind
    for m in (_ASSIGN_QUOTED if code else _ASSIGN).finditer(line):
        v = m.group(1)
        if any(c.isdigit() for c in v) and any(c.isalpha() for c in v) and not (_PLACEHOLDER if code else _DOC_PLACEHOLDER).search(v):
            return "a password, token or key assignment"
    for m in _DB_URL.finditer(line):  # scheme://user:password@host with a real-looking password (not postgres/password/pass1234 style)
        v = m.group(1)
        if any(c.isdigit() for c in v) and any(c.isalpha() for c in v) and not _PLACEHOLDER.search(v) \
                and not re.search(r"(?i)pass(word)?\d*$|^(postgres|mysql|root|admin|secret|user)\d*$", v):
            return "a database URL with a password"
    return None


def find(text: str, *, code: bool = False, _depth: int = 0) -> tuple[str, int] | None:
    """(kind, 1-based line) of the first credential-looking text, else None. `code=True` (files the builder writes) only
    flags quoted literal assignments that don't look like placeholders; documents (uploads) flag unquoted ones too."""
    for n, line in enumerate(text.split("\n"), 1):
        if (kind := _line_kind(line, code)):
            return kind, n
    if _depth == 0 and not code:
        for m in _B64_RUN.finditer(text):
            raw = m.group(0)
            try:
                dec = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            if (hit := find(dec, _depth=1)):
                return hit[0], text.count("\n", 0, m.start()) + 1
    return None


def refusal_for_upload(kind: str, line: int) -> str:
    return (f"That file looks like it contains a credential (line {line}: {kind}). Remove it and upload again; "
            "put secrets in Build secrets instead.")


def tool_refusal(kind: str, line: int) -> str:
    return (f"refused: this content looks like it contains a credential (line {line}: {kind}); the value was not written. "
            "Never put secrets in files. Read them from environment variables, ask the human to add them under Build secrets "
            "(ask_human), and use an obviously fake value in tests and examples.")
