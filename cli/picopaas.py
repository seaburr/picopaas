#!/usr/bin/env python3
"""PicoPaaS CLI. Env: PICOPAAS_URL (default https://picopaas.seaburr.io), PICOPAAS_TOKEN (pp_sk_ API key)."""
import argparse
import os
import sys
from typing import List, Optional

import httpx

DEFAULT_URL = "https://picopaas.seaburr.io"


class CliError(Exception):
    pass


def _client(transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    token = os.environ.get("PICOPAAS_TOKEN")
    if not token:
        raise CliError("PICOPAAS_TOKEN is not set (create a pp_sk_ API key in the UI)")
    return httpx.Client(
        base_url=os.environ.get("PICOPAAS_URL", DEFAULT_URL).rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        transport=transport,
    )


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail or resp.text or resp.reason_phrase)


def _request(c: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    try:
        resp = c.request(method, path, **kw)
    except httpx.HTTPError as exc:
        raise CliError(f"request failed: {exc.__class__.__name__}")
    if resp.status_code >= 400:
        raise CliError(f"{resp.status_code}: {_detail(resp)}")
    return resp


def _find_app(c: httpx.Client, name: str) -> dict:
    for a in _request(c, "GET", "/apps").json():
        if a["name"] == name:
            return a
    raise CliError(f"no app named {name!r}")


def _parse_secrets(pairs: List[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise CliError(f"--secret expects K=V, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_apply(c, args) -> None:
    try:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise CliError(f"cannot read {args.file}: {exc.strerror}")
    r = _request(c, "POST", "/apps/spec", json={"spec": text, "secrets": _parse_secrets(args.secret)}).json()
    print(f"{'created' if r['created'] else 'updated'} {r['name']}: https://{r['hostname']} ({r['status']})")


def cmd_apps(c, args) -> None:
    for a in _request(c, "GET", "/apps").json():
        print(f"{a['name']}\t{a['hostname']}\t{a['status']}")


def cmd_status(c, args) -> None:
    a = _find_app(c, args.name)
    print(f"name:     {a['name']}\nhostname: {a['hostname']}\nstatus:   {a['status']}\nimage:    {a['image']}")


def cmd_rotate(c, args) -> None:
    a = _find_app(c, args.name)
    target = "db" if args.db else "storage"
    _request(c, "POST", f"/apps/{a['id']}/rotate-credentials", json={"target": target})
    print(f"rotated {target} credentials for {a['name']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="picopaas")
    sub = p.add_subparsers(dest="command", required=True)
    ap = sub.add_parser("apply", help="create or update an app from a YAML spec")
    ap.add_argument("-f", "--file", required=True)
    ap.add_argument("--secret", action="append", default=[], metavar="K=V")
    ap.set_defaults(fn=cmd_apply)
    sub.add_parser("apps", help="list your apps").set_defaults(fn=cmd_apps)
    st = sub.add_parser("status", help="show an app")
    st.add_argument("name")
    st.set_defaults(fn=cmd_status)
    ro = sub.add_parser("rotate", help="rotate DB or storage credentials")
    ro.add_argument("name")
    g = ro.add_mutually_exclusive_group(required=True)
    g.add_argument("--db", action="store_true")
    g.add_argument("--storage", action="store_true")
    ro.set_defaults(fn=cmd_rotate)
    return p


def main(argv: Optional[List[str]] = None, transport: Optional[httpx.BaseTransport] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with _client(transport) as c:
            args.fn(c, args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
