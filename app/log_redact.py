"""Best-effort redaction of obvious secrets in platform log lines before an admin views or downloads them.

Defence in depth (admin downloads end up on laptops): it masks bearer tokens, JWTs, API keys, Fernet keys/tokens,
URL passwords, presigned-URL signatures, PEM bodies, session cookies and `password=...`-style pairs. It cannot catch
every format; components should not log secrets in the first place.
"""
import re

from app import config

MASK = "<redacted>"
_API_KEY = re.escape(config.API_KEY_PREFIX) + r"[0-9a-fA-F]{16,}"
_RULES = [
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\b(\"?\s*[:=]\s*\"?)(?:(?:bearer|basic|token)\s+)?[^\s\",;]+"), r"\1\2" + MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), MASK),  # JWT
    (re.compile(_API_KEY), MASK),
    (re.compile(r"\bgAAAA[A-Za-z0-9_=-]{20,}"), MASK),  # Fernet token
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])"), MASK),  # Fernet / urlsafe-b64 32-byte key
    (re.compile(r"(?i)(X-Amz-(?:Signature|Credential|Security-Token)|Signature|AWSAccessKeyId|sig)=[^&\s\"']+"), r"\1=" + MASK),
    (re.compile(r"(?<=://)([^/\s:@]+):([^@\s/]+)@"), r"\1:" + MASK + "@"),  # scheme://user:pass@host
    (re.compile(r"(?i)\b(set-cookie|cookie)\b(\"?\s*[:=]\s*\"?)[^\r\n]*"), r"\1\2" + MASK),
    (re.compile(r"\bsession=[A-Za-z0-9._-]{10,}"), "session=" + MASK),
    (re.compile(r"(?i)((?:[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|fernet)[\w.-]*)"
                r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&}]+"), r"\1" + MASK),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), MASK),
]


def redact_line(line: str) -> str:
    for rx, rep in _RULES:
        line = rx.sub(rep, line)
    return line
