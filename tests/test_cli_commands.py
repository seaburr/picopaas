"""CLI: every command, success and error paths, against an httpx MockTransport."""
import json

import httpx
import pytest

from app.spec import parse_spec
from cli import picopaas

APP = {"id": 5, "name": "demo", "hostname": "a.x.y", "status": "running", "image": "ghcr.io/e/d:1", "port": 8080,
       "want_db": True, "db_engine": "pg", "want_storage": True, "health_check_enabled": True,
       "health_check_path": "/up", "replicas": 2,
       "env": [{"key": "LOG_LEVEL", "value": "info", "is_secret": False},
               {"key": "DATABASE_URL", "value": "${{ db.url }}", "is_secret": False},
               {"key": "API_TOKEN", "value": "********", "is_secret": True}]}


def run(argv, routes, monkeypatch, capsys):
    """routes: {(METHOD, path): Response | callable(req)}; GET /apps defaults to [APP]."""
    seen = []
    table = {("GET", "/apps"): httpx.Response(200, json=[APP]), **routes}

    def handler(req):
        seen.append(req)
        r = table.get((req.method, req.url.path))
        if r is None:
            return httpx.Response(404, json={"detail": "nope"})
        return r(req) if callable(r) else r

    monkeypatch.setenv("PICOPAAS_TOKEN", "pp_sk_test")
    monkeypatch.setenv("PICOPAAS_URL", "https://pp.test")
    code = picopaas.main(argv, transport=httpx.MockTransport(handler))
    out = capsys.readouterr()
    return code, out.out, out.err, seen


def test_unknown_app_every_command(monkeypatch, capsys):
    for argv in (["status"], ["logs"], ["restart"], ["check-image"], ["export"], ["delete", "--yes"], ["events"],
                 ["versions"], ["usage"], ["domain"], ["rotate", "--db"], ["storage-delete", "--all"]):
        argv = [argv[0], "ghost", *argv[1:]]
        code, _, err, _ = run(argv, {}, monkeypatch, capsys)
        assert code == 1 and "No app named ghost" in err, argv
    code, _, err, _ = run(["rollback", "ghost", "sha256:a"], {}, monkeypatch, capsys)
    assert code == 1 and "No app named ghost" in err


def test_status_shows_engine_and_replicas(monkeypatch, capsys):
    code, out, _, _ = run(["status", "demo"], {}, monkeypatch, capsys)
    assert code == 0 and "PostgreSQL" in out and "replicas: 2" in out and "/up" in out


def test_logs(monkeypatch, capsys):
    body = {"lines": ["a", "b"], "pod": None, "note": None, "pods": ["p1", "p2"], "sections": []}
    code, out, err, seen = run(["logs", "demo", "--tail", "5", "--pod", "p1"],
                               {("GET", "/apps/5/logs"): httpx.Response(200, json=body)}, monkeypatch, capsys)
    assert code == 0 and out == "a\nb\n" and err == ""
    assert seen[-1].url.params["tail"] == "5" and seen[-1].url.params["pod"] == "p1"
    code, out, err, seen = run(["logs", "demo"], {("GET", "/apps/5/logs"): httpx.Response(200, json=body)}, monkeypatch, capsys)
    assert "2 pods" in err and "pod" not in seen[-1].url.params
    nolog = {"lines": [], "note": "No pod is running yet."}
    code, out, err, _ = run(["logs", "demo"], {("GET", "/apps/5/logs"): httpx.Response(200, json=nolog)}, monkeypatch, capsys)
    assert out == "" and "No pod is running yet." in err
    code, _, err, _ = run(["logs", "demo", "--pod", "zz"], {("GET", "/apps/5/logs"): httpx.Response(404, json={"detail": "Pod not found."})}, monkeypatch, capsys)
    assert code == 1 and err.strip() == "error: Pod not found."


def test_restart_and_check_image(monkeypatch, capsys):
    code, out, _, seen = run(["restart", "demo"], {("POST", "/apps/5/restart"): httpx.Response(200, json=APP)}, monkeypatch, capsys)
    assert code == 0 and "Restarting demo" in out and seen[-1].method == "POST"
    code, _, err, _ = run(["restart", "demo"], {("POST", "/apps/5/restart"): httpx.Response(409, json={"detail": "This app is suspended, so it can't be restarted."})}, monkeypatch, capsys)
    assert code == 1 and "suspended" in err
    route = ("POST", "/apps/5/check-image")
    code, out, _, _ = run(["check-image", "demo"], {route: httpx.Response(200, json={**APP, "status": "pending"})}, monkeypatch, capsys)
    assert "rolling out" in out
    code, out, _, _ = run(["check-image", "demo"], {route: httpx.Response(200, json=APP)}, monkeypatch, capsys)
    assert "already on the latest" in out
    code, out, _, _ = run(["check-image", "demo"], {route: httpx.Response(200, json={**APP, "image_update_error": "image is root"})}, monkeypatch, capsys)
    assert "skipped" in out and "image is root" in out
    code, _, err, _ = run(["check-image", "demo"], {route: httpx.Response(502, json={"detail": "Couldn't reach the registry."})}, monkeypatch, capsys)
    assert code == 1 and "registry" in err


def test_export_round_trips_through_parse_spec(monkeypatch, capsys, tmp_path):
    code, out, _, _ = run(["export", "demo"], {}, monkeypatch, capsys)
    assert code == 0
    spec = parse_spec(out)
    assert spec.name == "demo" and spec.port == 8080 and spec.secrets == ["API_TOKEN"]
    assert spec.resources.db and spec.resources.db_engine == "pg" and spec.resources.storage
    assert spec.healthCheck.enabled and spec.healthCheck.path == "/up"
    assert spec.env == {"LOG_LEVEL": "info", "DATABASE_URL": "${{ db.url }}"} and "********" not in out
    f = tmp_path / "app.yaml"
    code, out, _, _ = run(["export", "demo", "-o", str(f)], {}, monkeypatch, capsys)
    assert code == 0 and parse_spec(f.read_text()).name == "demo" and "not exported" in out
    code, _, err, _ = run(["export", "demo", "-o", str(tmp_path / "no" / "app.yaml")], {}, monkeypatch, capsys)
    assert code == 1 and "Can't write" in err


def test_export_minimal_app():
    text = picopaas.spec_from_app({"name": "m", "image": "i:1", "port": 3000})
    assert text == 'apiVersion: picopaas/v1\nname: "m"\nimage: "i:1"\nport: 3000\n'
    assert parse_spec(text).resources.db is False


def test_delete_needs_yes(monkeypatch, capsys):
    code, _, err, seen = run(["delete", "demo"], {}, monkeypatch, capsys)
    assert code == 1 and "--yes" in err and all(r.method == "GET" for r in seen)
    code, out, _, seen = run(["delete", "demo", "--yes"], {("DELETE", "/apps/5"): httpx.Response(204)}, monkeypatch, capsys)
    assert code == 0 and out.strip() == "Deleted demo." and seen[-1].method == "DELETE"
    code, _, err, _ = run(["delete", "demo", "--yes"], {("DELETE", "/apps/5"): httpx.Response(403, json={"detail": "suspended"})}, monkeypatch, capsys)
    assert code == 1 and err.strip() == "error: suspended"


def test_events_and_versions(monkeypatch, capsys):
    ev = [{"at": "2020-01-01T00:00:00Z", "kind": "oom", "message": "Ran out of memory."}]
    code, out, _, _ = run(["events", "demo"], {("GET", "/apps/5/events"): httpx.Response(200, json=ev)}, monkeypatch, capsys)
    assert "oom" in out and "Ran out of memory." in out
    code, out, _, _ = run(["events", "demo"], {("GET", "/apps/5/events"): httpx.Response(200, json=[])}, monkeypatch, capsys)
    assert out.strip() == "No restart events."
    vs = [{"digest": "sha256:aaa", "image_ref": "r:1", "created_at": "2020-01-01T00:00:00Z", "current": True},
          {"digest": "sha256:bbb", "image_ref": "r:0", "created_at": "2019-01-01T00:00:00Z", "current": False}]
    code, out, _, _ = run(["versions", "demo"], {("GET", "/apps/5/image-history"): httpx.Response(200, json=vs)}, monkeypatch, capsys)
    assert "sha256:aaa" in out and "(current)" in out and "rollback demo DIGEST" in out
    assert out.count("(current)") == 1
    code, out, _, _ = run(["versions", "demo"], {("GET", "/apps/5/image-history"): httpx.Response(200, json=[])}, monkeypatch, capsys)
    assert "No earlier versions" in out


def test_rollback(monkeypatch, capsys):
    route = ("POST", "/apps/5/rollback")
    code, out, _, seen = run(["rollback", "demo", "sha256:bbb"], {route: httpx.Response(200, json=APP)}, monkeypatch, capsys)
    assert code == 0 and "sha256:bbb" in out and json.loads(seen[-1].content) == {"digest": "sha256:bbb"}
    code, _, err, _ = run(["rollback", "demo", "sha256:zzz"], {route: httpx.Response(400, json={"detail": "That version isn't in this app's recent history."})}, monkeypatch, capsys)
    assert code == 1 and "recent history" in err


def test_usage(monkeypatch, capsys):
    MB = 1024 * 1024
    routes = {
        ("GET", "/apps/5/database/usage"): httpx.Response(200, json={"size_bytes": 2 * MB, "limit_bytes": 50 * MB, "over_quota": False}),
        ("GET", "/apps/5/storage/usage"): httpx.Response(200, json={"size_bytes": 101 * MB, "limit_bytes": 100 * MB, "over_quota": True,
                                                                     "objects": 3, "top_prefixes": [{"prefix": "up", "size_bytes": MB, "objects": 3}]}),
    }
    code, out, _, _ = run(["usage", "demo"], routes, monkeypatch, capsys)
    assert code == 0 and "2.0 / 50.0 MB" in out and "OVER CAP" in out and "up  1.0 MB, 3 objects" in out
    bare = [{"id": 1, "name": "bare", "hostname": "h", "status": "running", "image": "i"}]
    code, out, _, _ = run(["usage", "bare"], {("GET", "/apps"): httpx.Response(200, json=bare)}, monkeypatch, capsys)
    assert "no database or storage bucket" in out
    code, _, err, _ = run(["usage", "demo"], {("GET", "/apps/5/database/usage"): httpx.Response(502, json={"detail": "Couldn't reach the service."})}, monkeypatch, capsys)
    assert code == 1 and "Couldn't reach" in err


def test_domain(monkeypatch, capsys):
    pending = {**APP, "custom_domain": {"host": "b.example.com", "status": "pending", "error": None,
                                        "challenge_host": "_pp.b.example.com", "challenge_token": "tok", "cname_target": "lb.x.y"}}
    base = "/apps/5/domain"
    code, out, _, seen = run(["domain", "demo", "--set", "b.example.com"], {("PUT", base): httpx.Response(200, json=pending)}, monkeypatch, capsys)
    assert json.loads(seen[-1].content) == {"host": "b.example.com"}
    assert "_pp.b.example.com = tok" in out and "lb.x.y" in out and "--verify" in out
    failed = {**pending, "custom_domain": {**pending["custom_domain"], "error": "TXT not found"}}
    code, out, _, _ = run(["domain", "demo", "--verify"], {("POST", base + "/verify"): httpx.Response(200, json=failed)}, monkeypatch, capsys)
    assert "problem:  TXT not found" in out
    active = {**pending, "custom_domain": {**pending["custom_domain"], "status": "active"}}
    code, out, _, _ = run(["domain", "demo", "--verify"], {("POST", base + "/verify"): httpx.Response(200, json=active)}, monkeypatch, capsys)
    assert "b.example.com (active)" in out and "TXT" not in out
    code, out, _, _ = run(["domain", "demo"], {}, monkeypatch, capsys)
    assert "no custom domain" in out
    code, out, _, _ = run(["domain", "demo", "--remove"], {("DELETE", base): httpx.Response(200, json=APP)}, monkeypatch, capsys)
    assert "Removed the custom domain" in out
    code, _, err, _ = run(["domain", "demo", "--set", "x.y"], {("PUT", base): httpx.Response(404, json={"detail": "Custom domains are disabled."})}, monkeypatch, capsys)
    assert code == 1 and "disabled" in err
    with pytest.raises(SystemExit):
        picopaas.build_parser().parse_args(["domain", "demo", "--set", "a.b", "--remove"])


def test_apply_errors_and_outcomes(monkeypatch, capsys, tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("apiVersion: picopaas/v1\n")
    r = {"created": False, "name": "demo", "hostname": "a.x.y", "status": "running"}
    code, out, _, seen = run(["apply", "-f", str(f)], {("POST", "/apps/spec"): httpx.Response(200, json=r)}, monkeypatch, capsys)
    assert out.startswith("Updated demo (running) at https://a.x.y")
    bad = {**r, "created": True, "status": "error", "status_detail": "image is root"}
    code, out, _, _ = run(["apply", "-f", str(f)], {("POST", "/apps/spec"): httpx.Response(200, json=bad)}, monkeypatch, capsys)
    assert "but it failed: image is root" in out
    err422 = httpx.Response(422, json={"detail": [{"loc": ["body", "spec"], "msg": "Value error, too big"}]})
    code, _, err, _ = run(["apply", "-f", str(f)], {("POST", "/apps/spec"): err422}, monkeypatch, capsys)
    assert code == 1 and "spec: too big" in err
    code, _, err, _ = run(["apply", "-f", str(tmp_path / "missing.yaml")], {}, monkeypatch, capsys)
    assert code == 1 and "Can't read" in err
    code, _, err, _ = run(["apply", "-f", str(f), "--secret", "oops"], {}, monkeypatch, capsys)
    assert code == 1 and "NAME=value" in err


def test_rotate_and_storage_delete_variants(monkeypatch, capsys):
    code, _, err, _ = run(["rotate", "demo", "--db"], {("POST", "/apps/5/rotate-credentials"): httpx.Response(409, json={"detail": "still being created"})}, monkeypatch, capsys)
    assert code == 1 and "still being created" in err
    r = {"deleted": 5000, "size_bytes": 3 * 1024 * 1024, "truncated": True, "over_quota": True}
    code, out, _, _ = run(["storage-delete", "demo", "--all"], {("POST", "/apps/5/storage/delete"): httpx.Response(200, json=r)}, monkeypatch, capsys)
    assert "Run the command again" in out and "stays read-only" in out


def test_network_server_and_auth_errors(monkeypatch, capsys):
    def boom(req):
        raise httpx.ConnectError("refused")
    code, _, err, _ = run(["apps"], {("GET", "/apps"): boom}, monkeypatch, capsys)
    assert code == 1 and "Can't reach https://pp.test" in err
    def slow(req):
        raise httpx.ReadTimeout("slow")
    code, _, err, _ = run(["apps"], {("GET", "/apps"): slow}, monkeypatch, capsys)
    assert "Timed out" in err
    code, _, err, _ = run(["apps"], {("GET", "/apps"): httpx.Response(401, json={"detail": "x"})}, monkeypatch, capsys)
    assert "Not logged in" in err
    code, _, err, _ = run(["-v", "apps"], {("GET", "/apps"): httpx.Response(500, text="oops")}, monkeypatch, capsys)
    assert "500:" in err and "PicoPaaS had a problem" in err
    monkeypatch.delenv("PICOPAAS_TOKEN")
    assert picopaas.main(["apps"]) == 1 and "PICOPAAS_TOKEN is not set" in capsys.readouterr().err


def test_every_command_has_help(capsys):
    for sub in ("apply", "apps", "status", "logs", "restart", "check-image", "export", "delete", "events", "versions",
                "rollback", "usage", "domain", "rotate", "storage-delete"):
        with pytest.raises(SystemExit):
            picopaas.main([sub, "-h"])
        assert "usage: picopaas" in capsys.readouterr().out


def test_storage_upload_streams_to_presigned_url(monkeypatch, capsys, tmp_path):
    f = tmp_path / "bundle.tar.gz"
    f.write_bytes(b"x" * 3000)
    got = {}

    def presign(req):
        got["body"] = json.loads(req.content)
        return httpx.Response(200, json={"url": "https://pp-5.spaces.test/site/bundle.tar.gz?sig=1", "method": "PUT",
                                         "headers": {"Content-Length": "3000"}, "key": "site/bundle.tar.gz"})

    def put(req):
        got["put"] = (req.headers.get("content-length"), len(req.read()), "authorization" in req.headers)
        return httpx.Response(200)

    code, out, _, _ = run(["storage", "upload", "demo", str(f), "--key", "site/bundle.tar.gz"],
                          {("POST", "/apps/5/storage/presign"): presign, ("PUT", "/site/bundle.tar.gz"): put},
                          monkeypatch, capsys)
    assert code == 0 and "Uploaded" in out and "site/bundle.tar.gz" in out
    assert got["body"] == {"method": "PUT", "key": "site/bundle.tar.gz", "content_length": 3000}
    assert got["put"] == ("3000", 3000, False)  # no PicoPaaS token leaks to the storage host


def test_storage_upload_errors(monkeypatch, capsys, tmp_path):
    code, _, err, _ = run(["storage", "upload", "demo", str(tmp_path / "nope")], {}, monkeypatch, capsys)
    assert code == 1 and "Can't read" in err
    f = tmp_path / "a"
    f.write_bytes(b"x")
    code, _, err, _ = run(["storage", "upload", "demo", str(f)],
                          {("POST", "/apps/5/storage/presign"): httpx.Response(413, json={"detail": "only 0 bytes remain"})},
                          monkeypatch, capsys)
    assert code == 1 and "remain" in err


def test_storage_download(monkeypatch, capsys, tmp_path):
    out_file = tmp_path / "out.bin"
    routes = {("POST", "/apps/5/storage/presign"): httpx.Response(200, json={"url": "https://pp-5.spaces.test/a/b.bin?sig=1", "method": "GET", "headers": {}, "key": "a/b.bin"}),
              ("GET", "/a/b.bin"): httpx.Response(200, content=b"hello")}
    code, out, _, _ = run(["storage", "download", "demo", "a/b.bin", "-o", str(out_file)], routes, monkeypatch, capsys)
    assert code == 0 and out_file.read_bytes() == b"hello" and "Downloaded" in out
    routes[("GET", "/a/b.bin")] = httpx.Response(404)
    code, _, err, _ = run(["storage", "download", "demo", "a/b.bin", "-o", str(out_file)], routes, monkeypatch, capsys)
    assert code == 1 and "No such object" in err


def test_admin_backup_restore(monkeypatch, capsys):
    apps = [{"id": 5, "name": "demo", "owner_email": "a@x.y"}, {"id": 9, "name": "copy", "owner_email": "b@x.y"}]
    seen = {}

    def post(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(202, json={"id": 77})

    routes = {("GET", "/admin/apps"): httpx.Response(200, json=apps), ("POST", "/admin/backups/12/restore"): post}
    code, out, _, _ = run(["admin", "backup", "restore", "12", "--to", "copy"], routes, monkeypatch, capsys)
    assert code == 0 and "run 77" in out and seen["body"] == {"target_app_id": 9, "overwrite_source": False}
    code, out, _, _ = run(["admin", "backup", "restore", "12", "--to", "5", "--overwrite-source"], routes, monkeypatch, capsys)
    assert code == 0 and seen["body"] == {"target_app_id": 5, "overwrite_source": True}
    code, _, err, _ = run(["admin", "backup", "restore", "12", "--to", "ghost"], routes, monkeypatch, capsys)
    assert code == 1 and "No app named or numbered ghost" in err
    dup = {("GET", "/admin/apps"): httpx.Response(200, json=apps + [{"id": 10, "name": "copy", "owner_email": "c@x.y"}])}
    code, _, err, _ = run(["admin", "backup", "restore", "12", "--to", "copy"], dup, monkeypatch, capsys)
    assert code == 1 and "numeric id" in err
    refuse = {**routes, ("POST", "/admin/backups/12/restore"): httpx.Response(409, json={"detail": "That would overwrite the app."})}
    code, _, err, _ = run(["admin", "backup", "restore", "12", "--to", "5"], refuse, monkeypatch, capsys)
    assert code == 1 and "overwrite" in err


BK = {"feature_enabled": True, "configured": True, "enabled": False, "schedule": "daily", "state": "disabled",
      "last_success_at": None, "next_backup_at": None,
      "runs": [
          {"id": 31, "kind": "bucket", "trigger": "manual", "status": "succeeded", "created_at": "2026-10-24T01:00:00", "bytes_added": 2048},
          {"id": 30, "kind": "db", "trigger": "manual", "status": "failed", "created_at": "2026-10-24T00:50:00", "error": "database dump failed"},
          {"id": 29, "kind": "db", "trigger": "scheduled", "status": "succeeded", "created_at": "2026-10-23T03:00:00", "bytes_added": 1048576},
          {"id": 28, "kind": "db", "trigger": "scheduled", "status": "succeeded", "created_at": "2026-10-22T03:00:00", "bytes_added": 10}]}


def test_backup_list_run_enable_disable(monkeypatch, capsys):
    routes = {("GET", "/apps/5/backups"): httpx.Response(200, json=BK)}
    code, out, _, _ = run(["backup", "list", "demo"], routes, monkeypatch, capsys)
    assert code == 0 and "off (scheduled backups are opt-in" in out and "database dump failed" in out and "31" in out
    code, out, _, _ = run(["backup", "list", "demo"], {("GET", "/apps/5/backups"): httpx.Response(200, json={**BK, "feature_enabled": False})}, monkeypatch, capsys)
    assert code == 1
    routes = {("POST", "/apps/5/backups"): httpx.Response(202, json=[{"id": 40, "kind": "db"}, {"id": 41, "kind": "bucket"}])}
    code, out, _, _ = run(["backup", "run", "demo"], routes, monkeypatch, capsys)
    assert code == 0 and "db, bucket" in out
    routes = {("POST", "/apps/5/backups"): httpx.Response(429, json={"detail": "You can start one backup per hour."})}
    code, _, err, _ = run(["backup", "run", "demo"], routes, monkeypatch, capsys)
    assert code == 1 and "one backup per hour" in err
    bodies = []

    def patch(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={**BK, "enabled": bodies[-1]["enabled"]})

    routes = {("PATCH", "/apps/5/backups"): patch}
    code, out, _, _ = run(["backup", "enable", "demo"], routes, monkeypatch, capsys)
    assert code == 0 and bodies[-1] == {"enabled": True} and "on for demo" in out
    code, out, _, _ = run(["backup", "disable", "demo"], routes, monkeypatch, capsys)
    assert code == 0 and bodies[-1] == {"enabled": False} and "still run one" in out


def _download_routes(states):
    polls = iter(states)

    def poll(req):
        st = next(polls)
        if st == "ready":
            return httpx.Response(200, json={"export_id": 90, "status": "ready", "filename": "demo-db-backup29.sql.gz",
                                             "url": "https://bk.spaces.test/exports/5/90.sql.gz?sig=1"})
        return httpx.Response(200, json={"export_id": 90, "status": st, "error": "uploading the file failed" if st == "failed" else None})

    return {("GET", "/apps/5/backups"): httpx.Response(200, json=BK),
            ("POST", "/apps/5/backups/29/download"): httpx.Response(202, json={"export_id": 90, "status": "queued"}),
            ("GET", "/apps/5/backups/exports/90"): poll,
            ("GET", "/exports/5/90.sql.gz"): httpx.Response(200, content=b"dump")}


def test_backup_download_specific_run_polls_then_saves(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(picopaas, "_POLL_S", 0)
    out_file = tmp_path / "db.sql.gz"
    code, out, err, seen = run(["backup", "download", "demo", "29", "-o", str(out_file)],
                               _download_routes(["queued", "running", "ready"]), monkeypatch, capsys)
    assert code == 0 and out_file.read_bytes() == b"dump" and "Downloaded db backup 29" in out
    assert "queued" in err and "running" in err
    assert not any("pp_sk_" in str(r.url) for r in seen if r.url.host != "pp.test")  # the token never goes to the storage host


def test_backup_download_default_is_latest_of_each_kind(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(picopaas, "_POLL_S", 0)
    monkeypatch.chdir(tmp_path)
    routes = _download_routes(["ready"])
    # latest db = 29 (30 failed), latest bucket = 31
    routes[("POST", "/apps/5/backups/31/download")] = httpx.Response(202, json={"export_id": 91, "status": "queued"})
    routes[("GET", "/apps/5/backups/exports/91")] = httpx.Response(200, json={"export_id": 91, "status": "ready", "filename": "demo-bucket-backup31.tar.gz", "url": "https://bk.spaces.test/exports/5/91.tar.gz"})
    routes[("GET", "/exports/5/91.tar.gz")] = httpx.Response(200, content=b"tar")
    code, out, _, seen = run(["backup", "download", "demo"], routes, monkeypatch, capsys)
    assert code == 0, out
    assert (tmp_path / "demo-db-backup29.sql.gz").read_bytes() == b"dump" and (tmp_path / "demo-bucket-backup31.tar.gz").read_bytes() == b"tar"
    assert ("POST", "/apps/5/backups/30/download") not in [(r.method, r.url.path) for r in seen]
    code, _, err, _ = run(["backup", "download", "demo", "-o", "x"], routes, monkeypatch, capsys)
    assert code == 1 and "-o is ambiguous" in err


def test_backup_download_errors(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(picopaas, "_POLL_S", 0)
    code, _, err, _ = run(["backup", "download", "demo", "30"], _download_routes([]), monkeypatch, capsys)
    assert code == 1 and "not a successful" in err
    code, _, err, _ = run(["backup", "download", "demo", "29", "-o", str(tmp_path / "x")], _download_routes(["running", "failed"]), monkeypatch, capsys)
    assert code == 1 and "uploading the file failed" in err
    routes = _download_routes([])
    routes[("POST", "/apps/5/backups/29/download")] = httpx.Response(429, json={"detail": "You can prepare 3 downloads per hour."})
    code, _, err, _ = run(["backup", "download", "demo", "29"], routes, monkeypatch, capsys)
    assert code == 1 and "3 downloads per hour" in err
    none = {("GET", "/apps/5/backups"): httpx.Response(200, json={**BK, "runs": []})}
    code, _, err, _ = run(["backup", "download", "demo"], none, monkeypatch, capsys)
    assert code == 1 and "no successful backup" in err
