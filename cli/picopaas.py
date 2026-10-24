#!/usr/bin/env python3
"""PicoPaaS CLI. Env: PICOPAAS_URL (default https://platform.picopaas.xyz), PICOPAAS_TOKEN (pp_sk_ API key)."""
import argparse
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

import httpx

DEFAULT_URL = "https://platform.picopaas.xyz"


class CliError(Exception):
    pass


TIMEOUT_S = 30
_TRANSPORT: Optional[httpx.BaseTransport] = None  # tests inject one for the presigned-URL transfer
_VERBOSE = False
_POLL_S = 3  # seconds between checks while a backup download is being prepared
_DOWNLOAD_WAIT_S = 20 * 60
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
    if (a.get("replicas") or 1) > 1:
        lines.append(("replicas", str(a["replicas"])))
    if a.get("volume"):
        v = a["volume"]
        lines.append(("volume", f"{v['size_gi']} GiB at {v['mount_path']}" + (f" ({v['phase']})" if v.get("phase") else "")))
    if a.get("want_db"):
        lines.append(("engine", "PostgreSQL" if a.get("db_engine") == "pg" else "MySQL"))
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

def _presign(c, a: dict, body: dict) -> dict:
    return _request(c, "POST", f"/apps/{a['id']}/storage/presign", json=body).json()


def _chunks(f, size: int = 1024 * 1024):
    while True:
        b = f.read(size)
        if not b:
            return
        yield b


def cmd_storage_upload(c, args) -> None:
    a = _find_app(c, args.name)
    try:
        size = os.path.getsize(args.file)
    except OSError as exc:
        raise CliError(f"Can't read {args.file}: {exc.strerror or exc}")
    if size == 0:
        raise CliError(f"{args.file} is empty; nothing to upload.")
    key = args.key or os.path.basename(args.file)
    p = _presign(c, a, {"method": "PUT", "key": key, "content_length": size})
    headers = dict(p.get("headers") or {})
    headers["Content-Length"] = str(size)
    # The presigned URL is signed and points at Spaces, so it gets a plain client with no PicoPaaS token.
    with httpx.Client(timeout=None, transport=_TRANSPORT) as raw, open(args.file, "rb") as f:
        try:
            resp = raw.put(p["url"], content=_chunks(f), headers=headers)
        except httpx.HTTPError as exc:
            raise CliError(f"Upload failed ({exc.__class__.__name__}). Request a new link by running the command again.")
    if resp.status_code >= 300:
        raise CliError(f"Upload was rejected by the storage service ({resp.status_code}). "
                       "The link may have expired or the file size changed; try again.")
    print(f"Uploaded {args.file} ({_mb(size)} MB) to {a['name']}'s bucket as {p.get('key', key)}.")


def cmd_storage_download(c, args) -> None:
    a = _find_app(c, args.name)
    p = _presign(c, a, {"method": "GET", "key": args.key})
    out = args.output or os.path.basename(args.key.rstrip("/")) or "download"
    total = 0
    with httpx.Client(timeout=None, transport=_TRANSPORT) as raw:
        try:
            with raw.stream("GET", p["url"]) as resp:
                if resp.status_code >= 300:
                    raise CliError("No such object, or the link was rejected" f" ({resp.status_code}). Check the key.")
                with open(out, "wb") as f:
                    for b in resp.iter_bytes():
                        f.write(b)
                        total += len(b)
        except httpx.HTTPError as exc:
            raise CliError(f"Download failed ({exc.__class__.__name__}). Try again.")
    print(f"Downloaded {args.key} ({_mb(total)} MB) to {out}.")


def _mb(n) -> str:
    return f"{n / 1024 / 1024:.1f}"


def cmd_logs(c, args) -> None:
    a = _find_app(c, args.name)
    params = {"tail": args.tail}
    if args.pod:
        params["pod"] = args.pod
    r = _request(c, "GET", f"/apps/{a['id']}/logs", params=params).json()
    for line in r.get("lines") or []:
        print(line)
    if r.get("note"):
        print(f"note: {r['note']}", file=sys.stderr)
    if len(r.get("pods") or []) > 1 and not args.pod:
        print(f"note: {len(r['pods'])} pods; pick one with --pod ({', '.join(r['pods'])}).", file=sys.stderr)


def cmd_restart(c, args) -> None:
    a = _find_app(c, args.name)
    _request(c, "POST", f"/apps/{a['id']}/restart")
    print(f"Restarting {a['name']} (pulling the latest image for its tag first).")
    print(f"Check progress: picopaas status {a['name']}")


def cmd_check_image(c, args) -> None:
    a = _find_app(c, args.name)
    r = _request(c, "POST", f"/apps/{a['id']}/check-image").json()
    if r.get("image_update_error"):
        print(f"The new image was skipped, so the old one keeps running: {r['image_update_error']}")
    elif r.get("status") == "pending":
        print(f"{a['name']} is rolling out a new image. Check progress: picopaas status {a['name']}")
    else:
        print(f"{a['name']} is already on the latest image for its tag.")


def _yaml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def spec_from_app(a: dict) -> str:
    """Render an app (GET /apps) as a picopaas/v1 spec. Secret values are never exported, only their names."""
    env = [e for e in a.get("env") or [] if not e.get("is_secret")]
    secrets = [e["key"] for e in a.get("env") or [] if e.get("is_secret")]
    lines = ["apiVersion: picopaas/v1", f"name: {_yaml_str(a['name'])}", f"image: {_yaml_str(a['image'])}", f"port: {a['port']}"]
    if a.get("health_check_enabled"):
        lines.append(f"healthCheck: {{ enabled: true, path: {_yaml_str(a.get('health_check_path') or '/healthz')} }}")
    vol = a.get("volume")
    if a.get("want_db") or a.get("want_storage") or vol:
        parts = []
        if a.get("want_db"):
            parts.append("db: true")
            if a.get("db_engine") == "pg":
                parts.append("db_engine: pg")
        if a.get("want_storage"):
            parts.append("storage: true")
        if vol:
            parts.append(f"volume: {{ sizeGi: {vol['size_gi']}, mountPath: {_yaml_str(vol['mount_path'])} }}")
        lines.append(f"resources: {{ {', '.join(parts)} }}")
    if env:
        lines.append("env:")
        lines += [f"  {e['key']}: {_yaml_str(e['value'])}" for e in env]
    if secrets:
        lines.append("secrets:")
        lines += [f"  - {k}" for k in secrets]
    return "\n".join(lines) + "\n"


def cmd_export(c, args) -> None:
    text = spec_from_app(_find_app(c, args.name))
    if not args.output:
        print(text, end="")
        return
    try:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as exc:
        raise CliError(f"Can't write {args.output}: {(exc.strerror or 'unknown error').lower()}.")
    print(f"Wrote {args.output}. Secret values are not exported; pass them with --secret when you apply.")


def cmd_delete(c, args) -> None:
    a = _find_app(c, args.name)
    if not args.yes:
        raise CliError(
            f"This permanently deletes {a['name']} and destroys its database and storage data. "
            f"Run again with --yes to confirm: picopaas delete {a['name']} --yes"
        )
    _request(c, "DELETE", f"/apps/{a['id']}")
    print(f"Deleted {a['name']}.")


def cmd_events(c, args) -> None:
    a = _find_app(c, args.name)
    events = _request(c, "GET", f"/apps/{a['id']}/events").json()
    if not events:
        print("No restart events.")
        return
    for e in events:
        print(f"{_ago_text(e['at']):<10} {e['kind']:<8} {e['message']}")


def cmd_versions(c, args) -> None:
    a = _find_app(c, args.name)
    versions = _request(c, "GET", f"/apps/{a['id']}/image-history").json()
    if not versions:
        print("No earlier versions yet. Versions are recorded once an image has run successfully.")
        return
    for v in versions:
        mark = "  (current)" if v.get("current") else ""
        print(f"{v['digest']}  {_ago_text(v['created_at'])}  {v['image_ref']}{mark}")
    print(f"Roll back with: picopaas rollback {a['name']} DIGEST")


def cmd_rollback(c, args) -> None:
    a = _find_app(c, args.name)
    _request(c, "POST", f"/apps/{a['id']}/rollback", json={"digest": args.digest})
    print(f"Rolling {a['name']} back to {args.digest}. Check progress: picopaas status {a['name']}")


def cmd_usage(c, args) -> None:
    a = _find_app(c, args.name)
    shown = False
    for label, path, want in (("database", "database", a.get("want_db")), ("storage", "storage", a.get("want_storage"))):
        if not want:
            continue
        shown = True
        u = _request(c, "GET", f"/apps/{a['id']}/{path}/usage").json()
        text = f"{_mb(u['size_bytes'])} / {_mb(u['limit_bytes'])} MB"
        if u.get("over_quota"):
            text += "  OVER CAP, read-only"
        print(f"{label + ':':<10} {text}")
        for p in u.get("top_prefixes") or []:
            print(f"  {p['prefix']}  {_mb(p['size_bytes'])} MB, {p['objects']} objects")
    if not shown:
        print(f"{a['name']} has no database or storage bucket.")


def cmd_domain(c, args) -> None:
    a = _find_app(c, args.name)
    base = f"/apps/{a['id']}/domain"
    if args.remove:
        _request(c, "DELETE", base)
        print(f"Removed the custom domain from {a['name']}.")
        return
    if args.host:
        r = _request(c, "PUT", base, json={"host": args.host}).json()
    elif args.verify:
        r = _request(c, "POST", base + "/verify").json()
    else:
        r = a
    d = r.get("custom_domain")
    if not d:
        print(f"{a['name']} has no custom domain. Add one with: picopaas domain {a['name']} --set HOST")
        return
    print(f"domain:   {d['host']} ({d['status']})")
    if d["status"] != "active":
        print(f"TXT:      {d['challenge_host']} = {d['challenge_token']}")
        print(f"CNAME:    {d['host']} -> {d['cname_target']}")
        print(f"Then run: picopaas domain {a['name']} --verify")
    if d.get("error"):
        print(f"problem:  {d['error']}")


def _find_any_app(c: httpx.Client, ref: str) -> dict:
    """Platform admins: an app by name or numeric id across all owners (GET /admin/apps)."""
    apps = _request(c, "GET", "/admin/apps").json()
    hits = [a for a in apps if a["name"] == ref or str(a["id"]) == ref]
    if not hits:
        raise CliError(f"No app named or numbered {ref}.")
    if len(hits) > 1:
        ids = ", ".join(f"{a['id']} ({a['owner_email']})" for a in hits)
        raise CliError(f"More than one app is named {ref}: {ids}. Pass the numeric id.")
    return hits[0]


def cmd_admin_backup_restore(c, args) -> None:
    target = _find_any_app(c, args.to)
    body = {"target_app_id": target["id"], "overwrite_source": bool(args.overwrite_source)}
    run = _request(c, "POST", f"/admin/backups/{args.run_id}/restore", json=body).json()
    print(f"Restore queued as run {run['id']} into {target['name']} (app {target['id']}); it starts within a minute and "
          f"replaces what is there. Watch it: GET /apps/{target['id']}/backups")


def _backups(c, a: dict) -> dict:
    return _request(c, "GET", f"/apps/{a['id']}/backups").json()


def _need_backups(b: dict) -> None:
    if not b.get("feature_enabled"):
        raise CliError("Backups aren't enabled on this platform.")


def cmd_backup_list(c, args) -> None:
    a = _find_app(c, args.name)
    b = _backups(c, a)
    _need_backups(b)
    sched = f"{b['schedule']}" if b.get("enabled") else "off (scheduled backups are opt-in: picopaas backup enable " + a["name"] + ")"
    print(f"{a['name']}: scheduled backups {sched}; state {b['state']}")
    if b.get("last_success_at"):
        print(f"last success: {_ago_text(b['last_success_at'])}")
    if b.get("enabled") and b.get("next_backup_at"):
        print(f"next backup: {b['next_backup_at']}")
    runs = b.get("runs") or []
    if not runs:
        print("No backups yet. Run one with: picopaas backup run " + a["name"])
        return
    print(f"{'RUN':>6}  {'KIND':<8} {'HOW':<10} {'STATUS':<10} {'SIZE':>9}  WHEN")
    for r in runs:
        size = f"{_mb(r['bytes_added'])} MB" if r.get("bytes_added") is not None else "-"
        line = f"{r['id']:>6}  {r['kind']:<8} {r['trigger']:<10} {r['status']:<10} {size:>9}  {_ago_text(r['created_at'])}"
        print(line + (f"  ({r['error']})" if r.get("error") else ""))


def cmd_backup_run(c, args) -> None:
    a = _find_app(c, args.name)
    runs = _request(c, "POST", f"/apps/{a['id']}/backups").json()
    kinds = ", ".join(r["kind"] for r in runs)
    print(f"Backup queued for {a['name']} ({kinds}); it starts within a minute. Check it with: picopaas backup list {shlex.quote(a['name'])}")


def cmd_backup_enable(c, args) -> None:
    a = _find_app(c, args.name)
    on = args.backup_cmd == "enable"
    b = _request(c, "PATCH", f"/apps/{a['id']}/backups", json={"enabled": on}).json()
    if on:
        print(f"Scheduled backups are on for {a['name']} ({b['schedule']}). The first one starts within a minute or two.")
    else:
        print(f"Scheduled backups are off for {a['name']}. You can still run one with: picopaas backup run {shlex.quote(a['name'])}")


def _wait_export(c, a: dict, export_id: int) -> dict:
    deadline = time.monotonic() + _DOWNLOAD_WAIT_S
    shown = None
    while True:
        e = _request(c, "GET", f"/apps/{a['id']}/backups/exports/{export_id}").json()
        if e["status"] == "ready":
            return e
        if e["status"] in ("failed", "expired"):
            raise CliError(f"Preparing the download {e['status']}" + (f": {e['error']}" if e.get("error") else ".") + " Try again.")
        if e["status"] != shown:
            print(f"Preparing the download ({e['status']})...", file=sys.stderr)
            shown = e["status"]
        if time.monotonic() > deadline:
            raise CliError("The download is taking too long to prepare. Try again in a few minutes.")
        time.sleep(_POLL_S)


def _fetch(url: str, out: str) -> int:
    total = 0
    with httpx.Client(timeout=None, transport=_TRANSPORT) as raw:
        try:
            with raw.stream("GET", url) as resp:
                if resp.status_code >= 300:
                    raise CliError(f"The storage service refused the download ({resp.status_code}). Run the command again.")
                with open(out, "wb") as f:
                    for b in resp.iter_bytes():
                        f.write(b)
                        total += len(b)
        except httpx.HTTPError as exc:
            raise CliError(f"Download failed ({exc.__class__.__name__}). Try again.")
    return total


def cmd_backup_download(c, args) -> None:
    a = _find_app(c, args.name)
    b = _backups(c, a)
    _need_backups(b)
    ok = [r for r in b.get("runs") or [] if r["status"] == "succeeded" and r["trigger"] != "restore" and r["kind"] in ("db", "bucket")]
    if args.run_id is not None:
        picks = [r for r in ok if r["id"] == args.run_id]
        if not picks:
            raise CliError(f"Run {args.run_id} is not a successful database or blob storage backup of {a['name']}. "
                           f"See: picopaas backup list {shlex.quote(a['name'])}")
    else:
        picks, seen = [], set()
        for r in ok:  # newest first: the latest successful run of each kind
            if r["kind"] not in seen:
                seen.add(r["kind"])
                picks.append(r)
        if not picks:
            raise CliError(f"{a['name']} has no successful backup yet. Run one with: picopaas backup run {shlex.quote(a['name'])}")
    if args.output and len(picks) > 1:
        raise CliError("This app has more than one kind of backup, so -o is ambiguous. Pass a run id (see `picopaas backup list`).")
    for r in picks:
        e = _request(c, "POST", f"/apps/{a['id']}/backups/{r['id']}/download").json()
        e = _wait_export(c, a, e["export_id"])
        out = args.output or os.path.basename(e.get("filename") or "") or f"backup-{r['id']}"
        total = _fetch(e["url"], out)
        print(f"Downloaded {r['kind']} backup {r['id']} ({_mb(total)} MB) to {out}.")


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
    sg = sub.add_parser("storage", help="upload to or download from an app's storage bucket")
    ssub = sg.add_subparsers(dest="storage_command", required=True)
    up = ssub.add_parser("upload", help="upload a local file (streamed; limited by the bucket's remaining quota)")
    up.add_argument("name", help="the app's name")
    up.add_argument("file", help="local file to upload")
    up.add_argument("--key", help="object key (default: the file's name)")
    up.set_defaults(fn=cmd_storage_upload)
    dl = ssub.add_parser("download", help="download one object")
    dl.add_argument("name", help="the app's name")
    dl.add_argument("key", help="object key")
    dl.add_argument("-o", "--output", help="local file (default: the key's last segment)")
    dl.set_defaults(fn=cmd_storage_download)
    lg = sub.add_parser("logs", help="print an app's recent logs")
    lg.add_argument("name", help="the app's name")
    lg.add_argument("--tail", type=int, default=200, help="lines per pod (1 to 1000, default 200)")
    lg.add_argument("--pod", help="only this pod (see the note printed when an app has several)")
    lg.set_defaults(fn=cmd_logs)
    for cmd, fn, h in (
        ("restart", cmd_restart, "restart an app, pulling the latest image for its tag"),
        ("check-image", cmd_check_image, "re-check the image tag now and roll out a new image if there is one"),
        ("events", cmd_events, "show recent restart events (out of memory, crash, disk)"),
        ("versions", cmd_versions, "list the app's recent image versions"),
        ("usage", cmd_usage, "show database and storage usage"),
    ):
        sp = sub.add_parser(cmd, help=h)
        sp.add_argument("name", help="the app's name")
        sp.set_defaults(fn=fn)
    ex = sub.add_parser("export", help="print an app as a YAML spec (secret values are not exported)")
    ex.add_argument("name", help="the app's name")
    ex.add_argument("-o", "--output", help="write to this file instead of stdout")
    ex.set_defaults(fn=cmd_export)
    de = sub.add_parser("delete", help="permanently delete an app and its data")
    de.add_argument("name", help="the app's name")
    de.add_argument("--yes", action="store_true", help="confirm the deletion")
    de.set_defaults(fn=cmd_delete)
    rb = sub.add_parser("rollback", help="redeploy an earlier image version (see `versions`)")
    rb.add_argument("name", help="the app's name")
    rb.add_argument("digest", help="the version's digest, from `picopaas versions`")
    rb.set_defaults(fn=cmd_rollback)
    dm = sub.add_parser("domain", help="show, set, verify or remove an app's custom domain")
    dm.add_argument("name", help="the app's name")
    g3 = dm.add_mutually_exclusive_group()
    g3.add_argument("--set", dest="host", metavar="HOST", help="attach this domain and print the DNS records to add")
    g3.add_argument("--verify", action="store_true", help="check the DNS records and activate the domain")
    g3.add_argument("--remove", action="store_true", help="detach the custom domain")
    dm.set_defaults(fn=cmd_domain)
    bk = sub.add_parser("backup", help="list, run, enable or download an app's backups")
    bsub = bk.add_subparsers(dest="backup_cmd", required=True)
    bl = bsub.add_parser("list", help="show backup settings and recent runs")
    bl.add_argument("name", help="the app's name")
    bl.set_defaults(fn=cmd_backup_list)
    br = bsub.add_parser("run", help="start a backup now (one per app per hour)")
    br.add_argument("name", help="the app's name")
    br.set_defaults(fn=cmd_backup_run)
    for word, h in (("enable", "turn on scheduled (daily) backups"), ("disable", "turn off scheduled backups (manual backups still work)")):
        be = bsub.add_parser(word, help=h)
        be.add_argument("name", help="the app's name")
        be.set_defaults(fn=cmd_backup_enable)
    bd = bsub.add_parser("download", help="download a backup as one file (default: the latest successful run of each kind)")
    bd.add_argument("name", help="the app's name")
    bd.add_argument("run_id", nargs="?", type=int, help="a run id from `backup list` (default: latest of each kind)")
    bd.add_argument("-o", "--output", help="local file (default: the name the platform suggests)")
    bd.set_defaults(fn=cmd_backup_download)
    ad = sub.add_parser("admin", help="platform administrator commands")
    asub = ad.add_subparsers(dest="admin_command", required=True)
    ab = asub.add_parser("backup", help="backup helpers").add_subparsers(dest="backup_command", required=True)
    rs = ab.add_parser("restore", help="restore a database or blob storage backup into an app (replaces its data)")
    rs.add_argument("run_id", type=int, help="the backup run's id (GET /apps/ID/backups lists them)")
    rs.add_argument("--to", required=True, metavar="APP", help="target app (name or id); never the source unless --overwrite-source")
    rs.add_argument("--overwrite-source", action="store_true", help="allow restoring over the source app (it must be suspended)")
    rs.set_defaults(fn=cmd_admin_backup_restore)
    return p


def main(argv: Optional[List[str]] = None, transport: Optional[httpx.BaseTransport] = None) -> int:
    global _VERBOSE, _TRANSPORT
    _TRANSPORT = transport
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
