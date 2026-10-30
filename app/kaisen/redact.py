"""Mask stored secret values in anything shown or logged."""
import json

from app import config

MASK = "[REDACTED]"


def redact(text: str, secrets) -> str:
    for s in sorted({x for x in secrets if x and len(x) >= 6}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


def _clip(t: str, n: int = config.KAISEN_TRANSCRIPT_CLIP) -> str:
    return t if len(t) <= n else t[:n] + " …[truncated]"


def transcript_lines(messages: list, secrets) -> list[tuple[str, str]]:
    """Flatten stored messages into (role, text) pairs, redacted. Thinking blocks are not shown."""
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append((m["role"], redact(_clip(content), secrets)))
            continue
        for b in content or []:
            t = b.get("type")
            if t == "text":
                txt = b.get("text", "")
            elif t == "tool_use":
                txt = f"[call] {b.get('name')} {json.dumps(b.get('input'), default=str)}"
            elif t == "server_tool_use":  # web search/fetch: show what was looked up, never the page bodies
                inp = b.get("input") or {}
                txt = (f"[web] searched for {inp.get('query')}" if b.get("name") == "web_search"
                       else f"[web] read {inp.get('url')}")
            elif t == "tool_result":
                c = b.get("content")
                txt = "[result] " + (c if isinstance(c, str) else json.dumps(c, default=str))
            else:
                continue
            out.append((m["role"], redact(_clip(txt), secrets)))
    return out
