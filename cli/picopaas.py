#!/usr/bin/env python3
"""PicoPaaS CLI. Env: PICOPAAS_URL (default https://platform.picopaas.xyz), PICOPAAS_TOKEN (pp_sk_ API key)."""
import argparse
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

import httpx

DEFAULT_URL = "https://platform.picopaas.xyz"


class CliError(Exception):
    pass


TIMEOUT_S = 30
_VERBOSE = False
# What the UI calls these states (the API keeps the raw values).
_STATUS_WORDS = {"error": "failed", "pending": "queued"}


def _base_url() -> str:
    return os.environ.get("PICOPAAS_URL", DEFAULT_URL).rstrip("/")


def _client(transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    token = os.environ.get("PICOPAAS_TOKEN")
    if not token:
        raise CliError(
            f"PICOPAAS_TOKEN is not set. Create an API key at {_base_url()}/keys and run: "
            "export PICOPAAS_TOKEN=pp_sk_..."
        )
    return httpx.Client(
        base_url=_base_url(),
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT_S,
        transport=transport,
    )


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(_validation_line(d) for d in detail)
    return str(detail or resp.text or resp.reason_phrase)


def _validation_line(d) -> str:
    if not isinstance(d, dict):
        return str(d)
    loc = [str(p) for p in d.get("loc", []) if p != "body"]
    msg = str(d.get("msg", d)).removeprefix("Value error, ")
    return f"{'.'.join(loc)}: {msg}" if loc else msg


def _network_error(exc: httpx.HTTPError) -> str:
    url = _base_url()
    if isinstance(exc, httpx.TimeoutException):
        return f"Timed out after {TIMEOUT_S}s waiting for {url}. Try again, and check PICOPAAS_URL."
    reason = str(exc).strip()
    cause = f"{exc.__class__.__name__}: {reason}" if reason else exc.__class__.__name__
    return f"Can't reach {url} ({cause}). Check your network and PICOPAAS_URL."


def _http_error(resp: httpx.Response) -> str:
    code, detail = resp.status_code, _detail(resp)
    if code == 401:
        msg = (f"Not logged in. Your PICOPAAS_TOKEN is missing, wrong or revoked. "
               f"Create a new key at {_base_url()}/keys.")
    elif code >= 500:
        msg = f"PicoPaaS had a problem ({code}): {detail} Try again; if it keeps happening, tell an administrator."
    else:
        msg = detail  # 4xx details are written to be read as-is (what happened, why, what to do)
    return f"{code}: {msg}" if _VERBOSE and code != 401 else msg


def _request(c: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    try:
        resp = c.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise CliError(_network_error(exc))
    if resp.status_code >= 400:
        raise CliError(_http_error(resp))
    return resp


def _find_app(c: httpx.Client, name: str) -> dict:
    for a in _request(c, "GET", "/apps").json():
        if a["name"] == name:
            return a
    raise CliError(f"No app named {name}. Run `picopaas apps` to see your apps.")


def _parse_secrets(pairs: List[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise CliError(f'--secret needs NAME=value, for example --secret API_KEY=abc123 (got "{p}").')
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _word(status: str) -> str:
    return _STATUS_WORDS.get(status, status)


def _retry_text(retry_at: Optional[str]) -> Optional[str]:
    if not retry_at:
        return None
    try:
        when = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
    except ValueError:
        return retry_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    secs = int((when - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "any moment now"
    mins = (secs + 59) // 60
    return f"in about {mins} min" if mins > 1 else "in under a minute"


def _ago_text(iso: str) -> str:
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - when).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    return f"{secs // 3600} h ago"


def cmd_apply(c, args) -> None:
    try:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise CliError(f"Can't read {args.file}: {(exc.strerror or 'unknown error').lower()}. Check the path passed to -f.")
    r = _request(c, "POST", "/apps/spec", json={"spec": text, "secrets": _parse_secrets(args.secret)}).json()
    verb = "Created" if r["created"] else "Updated"
    word = _word(r["status"])
    url = f"https://{r['hostname']}"
    if r["status"] == "error":
        print(f"{verb} {r['name']}, but it {word}: {r.get('status_detail') or 'no reason given'}")
    elif r["status"] == "running":
        print(f"{verb} {r['name']} ({word}) at {url}")
    else:
        print(f"{verb} {r['name']} ({word}). It'll be live in about a minute at {url}")
    print(f"Check progress: picopaas status {r['name']}")


def cmd_apps(c, args) -> None:
    apps = _request(c, "GET", "/apps").json()
    if not apps:
        print("No apps yet. Create one with: picopaas apply -f app.yaml")
        return
    rows = [("NAME", "URL", "STATUS")] + [(a["name"], f"https://{a['hostname']}", _word(a["status"])) for a in apps]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip())


def cmd_status(c, args) -> None:
    a = _find_app(c, args.name)
    health = a.get("health_check_path") if a.get("health_check_enabled") else "off"
    lines = [
        ("name", a["name"]),
        ("url", f"https://{a['hostname']}"),
        ("status", _word(a["status"])),
    ]
    if a.get("status_detail"):
        lines.append(("detail", a["status_detail"]))
    retry = _retry_text(a.get("retry_at"))
    if retry:
        lines.append(("retry", retry))
    ev = a.get("last_event")
    if ev:
        lines.append(("event", f"{_ago_text(ev['at'])}: {ev['message']}"))
    lines += [("image", a["image"]), ("health", health or "off")]
    for label, key, name in (("database", "db", "Database"), ("storage", "storage", "Storage")):
        u = (a.get("quota") or {}).get(key)
        if u:
            mb = lambda n: f"{n / 1024 / 1024:.1f}"  # noqa: E731
            text = f"{mb(u['size_bytes'])} / {mb(u['limit_bytes'])} MB"
            if u["over_quota"]:
                hint = "delete rows or drop tables" if key == "db" else "picopaas storage-delete " + a["name"] + " --all"
                text += f"  OVER CAP, read-only ({hint})"
            lines.append((label, text))
    for k, v in lines:
        print(f"{k + ':':<9} {v}")


def cmd_rotate(c, args) -> None:
    a = _find_app(c, args.name)
    target = "db" if args.db else "storage"
    _request(c, "POST", f"/apps/{a['id']}/rotate-credentials", json={"target": target})
    what = "database password" if target == "db" else "storage bucket keys"
    print(f"Rotated the {what} for {a['name']}. The app is restarting.")


def cmd_storage_delete(c, args) -> None:
    a = _find_app(c, args.name)
    body = {"all": True} if args.all else {"prefix": args.prefix}
    r = _request(c, "POST", f"/apps/{a['id']}/storage/delete", json=body).json()
    left = r["size_bytes"] / 1024 / 1024
    print(f"Deleted {r['deleted']} objects from {a['name']}'s bucket ({left:.1f} MB left).")
    if r.get("truncated"):
        print("More objects remain. Run the command again to continue.")
    if r.get("over_quota"):
        print("The bucket is still over its cap, so it stays read-only.")
    else:
        print("Writes are restored; the app restarts to pick up the new keys.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="picopaas",
        description="Deploy and manage PicoPaaS apps.",
        epilog=(
            "environment:\n"
            "  PICOPAAS_TOKEN  your pp_sk_ API key (create one at <PICOPAAS_URL>/keys)\n"
            f"  PICOPAAS_URL    PicoPaaS address (default {DEFAULT_URL})"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="show HTTP status codes in error messages")
    sub = p.add_subparsers(dest="command", required=True)
    ap = sub.add_parser("apply", help="create or update an app from a YAML spec")
    ap.add_argument("-f", "--file", required=True, help="path to the YAML spec")
    ap.add_argument("--secret", action="append", default=[], metavar="NAME=value",
                    help="supply a secret declared in the spec (repeatable)")
    ap.set_defaults(fn=cmd_apply)
    sub.add_parser("apps", help="list your apps").set_defaults(fn=cmd_apps)
    st = sub.add_parser("status", help="show an app's state, URL and, if it failed, why")
    st.add_argument("name", help="the app's name")
    st.set_defaults(fn=cmd_status)
    ro = sub.add_parser("rotate", help="rotate an app's database or storage credentials")
    ro.add_argument("name", help="the app's name")
    g = ro.add_mutually_exclusive_group(required=True)
    g.add_argument("--db", action="store_true", help="rotate the database password")
    g.add_argument("--storage", action="store_true", help="rotate the storage bucket keys")
    ro.set_defaults(fn=cmd_rotate)
    sd = sub.add_parser("storage-delete", help="delete objects from an app's storage bucket (frees an over-cap bucket)")
    sd.add_argument("name", help="the app's name")
    g2 = sd.add_mutually_exclusive_group(required=True)
    g2.add_argument("--prefix", help="delete only objects whose key starts with this")
    g2.add_argument("--all", action="store_true", help="delete every object in the bucket")
    sd.set_defaults(fn=cmd_storage_delete)
    return p


def main(argv: Optional[List[str]] = None, transport: Optional[httpx.BaseTransport] = None) -> int:
    global _VERBOSE
    args = build_parser().parse_args(argv)
    _VERBOSE = args.verbose
    try:
        with _client(transport) as c:
            args.fn(c, args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
