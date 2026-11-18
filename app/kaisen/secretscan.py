"""High-confidence credential detection shared by the builder tools and the upload route (docs/design/kaisen.md 16.10).
Returns the kind and the line number, never the value. A heuristic: custom secrets slip through, so it favours
precision (few false alarms on ordinary code) over recall."""
import base64
import html
import re
import unicodedata
import urllib.parse

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
        # short or unaligned base64 the 60-character pass above does not decode, then the disguises find_token undoes
        return _encoded_token(text, 0) or find_token(text)
    return None


_B64_ANY = re.compile(r"[A-Za-z0-9+/_-]{16,}")


# Decoded base64 has no word boundaries to rely on: the token may follow any byte.
_PATTERNS_DECODED = [(kind, re.compile(rx.pattern.removeprefix(r"\b"))) for kind, rx in _PATTERNS]


def _token_kind(line: str, decoded: bool = False) -> str | None:
    for kind, rx in (_PATTERNS_DECODED if decoded else _PATTERNS):
        m = rx.search(line)
        if m and "EXAMPLE" not in m.group(0):
            return kind
    return None


_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")
_WS = re.compile(r"\s+")


def _encoded_token(text: str, depth: int) -> tuple[str, int] | None:
    """`_encoded_token_runs`, then once more with whitespace removed: `base64` output is wrapped at 76 columns and a
    person may paste it with line breaks or spaces inside."""
    if (hit := _encoded_token_runs(text, depth)):
        return hit
    squashed = _WS.sub("", text)
    if depth == 0 and squashed != text:
        hit = _encoded_token_runs(squashed, depth)
        return (hit[0], 1) if hit else None
    return None


def _encoded_token_runs(text: str, depth: int) -> tuple[str, int] | None:
    """A provider token inside base64 (standard or URL-safe alphabet, padded or not, up to two layers). Each run is
    decoded at all four alignments, so a token is found wherever it sits in the encoded text and whatever is glued in
    front of the run."""
    for m in _B64_ANY.finditer(text):
        run = m.group(0).replace("-", "+").replace("_", "/")
        for off in range(4):
            part = run[off:]
            if len(part) % 4 == 1:
                part = part[:-1]
            try:
                dec = base64.b64decode(part + "=" * (-len(part) % 4)).decode("latin-1")
            except ValueError:
                continue
            kind = _token_kind(dec, decoded=True)
            if kind is None and depth < 1 and (hit := _encoded_token(dec, depth + 1)):
                kind = hit[0]
            if kind:
                return (kind if kind.endswith("(base64-encoded)") else kind + " (base64-encoded)",
                        text.count("\n", 0, m.start()) + 1)
    return None


_HEX_RUN = re.compile(r"(?:[0-9a-fA-F]{2}){16,}")
_B32_RUN = re.compile(r"[A-Za-z2-7]{26,}={0,6}")


def _normalize(text: str) -> str:
    """Undo cheap disguises: fullwidth and other compatibility forms (NFKD), zero-width, bidi and tag characters and
    combining marks, HTML entities and percent-encoding."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cf", "Mn", "Me"))
    return urllib.parse.unquote(html.unescape(t))


def _hex_base32_token(text: str) -> str | None:
    """A provider token written as hex digits or as base32."""
    for m in _HEX_RUN.finditer(text):
        try:
            dec = bytes.fromhex(m.group(0)).decode("latin-1")
        except ValueError:
            continue
        if (kind := _token_kind(dec, decoded=True)):
            return kind + " (hex-encoded)"
    for m in _B32_RUN.finditer(text):
        raw = m.group(0).upper().rstrip("=")
        try:
            dec = base64.b32decode(raw + "=" * (-len(raw) % 8)).decode("latin-1")
        except ValueError:
            continue
        if (kind := _token_kind(dec, decoded=True)):
            return kind + " (base32-encoded)"
    return None


def find_token(text: str) -> tuple[str, int] | None:
    """(kind, 1-based line) of the first provider token or private key, plain or encoded (base64, hex, base32, percent,
    HTML entities, fullwidth or zero-width disguised), else None. For text a person types into the Kaisen conversation
    (brief, change request, answer, names): prefixed tokens only, none of the assignment heuristics of `find`, so
    ordinary prose about passwords and settings is not refused. Not caught, by design: a token split across lines or
    messages, reversed, interleaved with ordinary characters, rot13, or written with look-alike letters from another
    alphabet. The block is a guard against pasting a key by accident, not a data-loss-prevention system."""
    text = _INVISIBLE.sub("", text)  # a zero-width character inside a key must not hide it
    for cand in dict.fromkeys((text, _normalize(text))):
        for n, line in enumerate(cand.split("\n"), 1):
            if (kind := _token_kind(line)):
                return kind, n
        if (hit := _encoded_token(cand, 0)):
            return hit
        if (kind := _hex_base32_token(cand)):
            return kind, 1
    return None


def chat_refusal(kind: str, line: int) -> str:
    return (f"This text looks like it contains a credential (line {line}: {kind}), so it was not sent. Remove it and try "
            "again. A value the app needs goes under Build secrets, never in the conversation.")


def refusal_for_upload(kind: str, line: int) -> str:
    return (f"That file looks like it contains a credential (line {line}: {kind}). Remove it and upload again; "
            "put secrets in Build secrets instead.")


def tool_refusal(kind: str, line: int) -> str:
    return (f"refused: this content looks like it contains a credential (line {line}: {kind}); the value was not written. "
            "Never put secrets in files. Read them from environment variables, ask the human to add them under Build secrets "
            "(ask_human), and use an obviously fake value in tests and examples.")
