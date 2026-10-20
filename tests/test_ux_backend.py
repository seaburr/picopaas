"""UX chunk A1: cache headers, plain-language errors, enumeration-safe register, /auth/me extras,
password reset/change, CLI wording."""
import json

import httpx
import pytest

from app import config, main as app_main, models
from cli import picopaas
from tests.conftest import DEFAULT_PASSWORD, register_and_login

SPEC = """apiVersion: picopaas/v1
name: demo
image: ghcr.io/example/demo:1.0
port: 8080
"""


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def user(make_client, admin):
    c = make_client()
    register_and_login(c, "user@example.com")
    return c


# --- UX-1: cache headers / SPA vs JSON ---

def test_json_responses_are_no_store_and_vary(admin):
    r = admin.get("/auth/me")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["vary"] == "Sec-Fetch-Mode, Accept"
    assert admin.get("/healthz").headers["cache-control"] == "no-store"


def test_spa_html_is_no_cache_and_vary(client, tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    index.write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(app_main, "_INDEX_HTML", index)
    r = client.get("/apps", headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
    assert "<title>spa</title>" in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["vary"] == "Sec-Fetch-Mode, Accept"
    r = client.get("/some/client/route")
    assert r.headers["cache-control"] == "no-cache"


def test_json_accept_never_gets_spa(client, tmp_path, monkeypatch):
    index = tmp_path / "index.html"
    index.write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(app_main, "_INDEX_HTML", index)
    r = client.get("/apps", headers={"Accept": "application/json", "Sec-Fetch-Mode": "navigate"})
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
    r = client.get("/apps", headers={"Accept": "application/json"})
    assert r.status_code == 401


# --- Plain-language errors ---

def test_auth_error_copy(client, admin, make_client):
    anon = make_client()
    assert anon.get("/apps").json()["detail"].startswith("You're logged out.")
    r = anon.post("/auth/login", json={"email": "admin@example.com", "password": "not-the-password"})
    assert r.status_code == 401 and r.json()["detail"].startswith("Email or password is wrong.")
    r = anon.post("/auth/login", json={"email": "nobody@example.com", "password": "not-the-password"})
    assert r.json()["detail"].startswith("Email or password is wrong.")  # same for unknown email


def test_admin_required_copy(user):
    assert user.get("/admin/users").json()["detail"] == "This needs admin access."


def test_not_found_and_limit_copy(user):
    assert user.delete("/apps/999").json()["detail"] == "We can't find that app. It may have been deleted."
    assert user.delete("/apps/api-keys/999").status_code in (404, 405)
    r = user.post("/apps", json={"name": "a", "image": "x/y:1", "port": 80})
    assert r.status_code == 201
    r = user.post("/apps", json={"name": "b", "image": "x/y:1", "port": 80})
    assert r.status_code == 403
    assert r.json()["detail"] == "You've reached your limit of 1 app. Delete one, or ask an admin to raise your limit."


def test_duplicate_env_and_root_image_copy(user, admin):
    env = [{"key": "FOO", "value": "1"}, {"key": "FOO", "value": "2"}]
    r = user.post("/apps", json={"name": "a", "image": "x/y:1", "port": 80, "env": env})
    assert r.json()["detail"] == "FOO is listed twice. Env var names must be unique."
    r = user.post("/apps", json={"name": "a", "image": "x/y:1", "port": 80,
                                 "env": [{"key": "bad-key", "value": "1"}]})
    assert r.status_code == 422 and "UPPER_SNAKE_CASE" in r.text


def test_api_key_not_found_copy(user):
    r = user.delete("/api/keys/999")
    assert r.status_code == 404 and "already be revoked" in r.json()["detail"]


def test_admin_conflict_copy(admin, user):
    uid = user.get("/auth/me").json()["id"]
    r = admin.post(f"/admin/users/{uid}/approve")
    assert r.status_code == 409 and r.json()["detail"] == "This user was already approved. Refresh the list."
    r = admin.post(f"/admin/users/{uid}/unsuspend")
    assert r.json()["detail"] == "This user isn't suspended. Refresh the list."
    assert admin.post("/admin/users/1/suspend").json()["detail"] == "The main admin account can't be suspended or deleted."
    assert "can't find that user" in admin.post("/admin/users/999/approve").json()["detail"]


def test_spec_error_copy(user):
    def post(spec, secrets=None):
        return user.post("/apps/spec", json={"spec": spec, "secrets": secrets or {}})

    d = post("a: [unclosed\n  b: :").json()["detail"]
    assert d.startswith("Your spec isn't valid YAML (line") and "ScannerError" not in d and "Check indentation" in d
    assert post("a: &x 1\nb: *x").json()["detail"] == (
        "Specs can't use YAML anchors or aliases (& and *). Write the values out in full.")
    assert post("# " + "x" * 70000).status_code == 422
    d = post(SPEC.replace("port: 8080\n", "")).json()["detail"]
    assert d == "port: is required"
    d = post(SPEC + "bogus: 1\n").json()["detail"]
    assert d == "bogus: isn't a known field"
    d = post(SPEC + "env:\n  A: x\nsecrets: [B]\n", {"C": "1", "B": "2"}).json()["detail"]
    assert d == ("You provided values for C, but they aren't declared under secrets: in the spec. "
                 "Add them there or remove the values.")
    d = post(SPEC + "secrets: [B]\n").json()["detail"]
    assert d == ("Missing values for these secrets: B. Pass --secret NAME=value (CLI) "
                 "or fill them in the secrets box (YAML tab).")
    d = post(SPEC + "env:\n  bad-key: x\n").json()["detail"]
    assert "UPPER_SNAKE_CASE" in d and "Value error" not in d
    assert post(SPEC + "healthCheck: {enabled: true, path: nope}\n").json()["detail"].startswith(
        "healthCheck, path: Health check path must start with /")
    assert post(SPEC.replace("picopaas/v1", "v2")).status_code == 422


def test_spec_cannot_remove_resources_copy(user):
    assert user.post("/apps/spec", json={"spec": SPEC + "resources: {db: true}\n"}).status_code == 200
    r = user.post("/apps/spec", json={"spec": SPEC})
    assert r.status_code == 422 and r.json()["detail"].startswith("A spec can't remove a database or storage bucket")


def test_ca_cert_copy(admin):
    r = admin.post("/admin/db-servers", json={"name": "x", "host": "h", "admin_user": "u",
                                              "admin_password": "not-the-password", "max_databases": 5,
                                              "ca_cert": "nope"})
    assert r.status_code == 422 and "-----BEGIN CERTIFICATE-----" in r.text


# --- Enumeration-safe register ---

def test_register_duplicate_is_indistinguishable(make_client, admin):
    payload = lambda e: {"email": e, "password": DEFAULT_PASSWORD, "accept_terms": True}  # noqa: E731
    fresh = make_client().post("/auth/register", json=payload("new@example.com"))
    dupe = make_client().post("/auth/register", json=payload("admin@example.com"))  # an existing account
    assert fresh.status_code == dupe.status_code == 201
    assert set(fresh.json()) == set(dupe.json())
    for k in ("role", "app_limit", "status"):
        assert fresh.json()[k] == dupe.json()[k]
    assert dupe.json()["status"] == "pending" and dupe.json()["role"] == "user"
    assert "session" in fresh.cookies and "session" in dupe.cookies


def test_register_duplicate_grants_nothing(make_client, admin):
    c = make_client()
    r = c.post("/auth/register", json={"email": "admin@example.com", "password": "not-the-password",
                                       "accept_terms": True})
    assert r.status_code == 201
    assert c.get("/auth/me").status_code == 401  # the cookie authenticates nobody
    assert c.get("/admin/users").status_code == 401
    # a later login with the wrong password fails with the generic message; the real password still works
    bad = c.post("/auth/login", json={"email": "admin@example.com", "password": "not-the-password"})
    assert bad.status_code == 401 and bad.json()["detail"].startswith("Email or password is wrong.")
    assert c.post("/auth/login", json={"email": "admin@example.com", "password": DEFAULT_PASSWORD}).status_code == 200
    # the original password was not replaced
    actions = [a["action"] for a in admin.get("/admin/audit").json()]
    assert "user.register_duplicate" in actions


# --- /auth/me features + limits ---

def test_me_features_and_limits(admin, db_session, monkeypatch):
    for k in ("DIGITALOCEAN_TOKEN", "SPACES_ACCESS_KEY_ID", "SPACES_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    me = admin.get("/auth/me").json()
    assert me["features"] == {"databases": False, "storage": False}
    assert me["limits"] == {
        "cpu_limit_m": config.DEFAULT_CPU_LIMIT_M, "mem_limit_mi": config.DEFAULT_MEM_LIMIT_MI,
        "cpu_request_m": config.DEFAULT_CPU_REQUEST_M, "mem_request_mi": config.DEFAULT_MEM_REQUEST_MI,
        "image_mb": config.MAX_IMAGE_SIZE_BYTES // 1048576, "db_mb": config.DB_QUOTA_BYTES // 1048576,
        "bucket_mb": config.STORAGE_QUOTA_BYTES // 1048576,
        "override": {"cpu_min_m": 10, "cpu_max_m": 1000, "mem_min_mi": 32, "mem_max_mi": 2048},
    }
    assert me["must_change_password"] is False
    db_session.add(models.DbServer(name="primary", host="h", admin_user="u", admin_password_encrypted="x"))
    db_session.commit()
    for k in ("DIGITALOCEAN_TOKEN", "SPACES_ACCESS_KEY_ID", "SPACES_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "not-a-secret")
    assert admin.get("/auth/me").json()["features"] == {"databases": True, "storage": True}


# --- Password reset / change ---

def test_admin_reset_password_flow(admin, user, make_client, db_session):
    uid = user.get("/auth/me").json()["id"]
    r = admin.post(f"/admin/users/{uid}/reset-password")
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert len(temp) >= 8
    c = make_client()
    assert c.post("/auth/login", json={"email": "user@example.com", "password": DEFAULT_PASSWORD}).status_code == 401
    assert c.post("/auth/login", json={"email": "user@example.com", "password": temp}).status_code == 200
    assert c.get("/auth/me").json()["must_change_password"] is True
    # audited, without the password
    entries = admin.get("/admin/audit").json()
    reset = [e for e in entries if e["action"] == "user.reset_password"]
    assert reset and str(uid) == str(reset[0]["target_id"])
    assert temp not in json.dumps(entries)

    # change it
    r = c.post("/auth/change-password", json={"current_password": temp, "new_password": "a-brand-new-pass"})
    assert r.status_code == 204
    assert c.get("/auth/me").json()["must_change_password"] is False
    assert make_client().post("/auth/login", json={"email": "user@example.com", "password": temp}).status_code == 401
    assert make_client().post("/auth/login",
                              json={"email": "user@example.com", "password": "a-brand-new-pass"}).status_code == 200
    assert "user.change_password" in [e["action"] for e in admin.get("/admin/audit").json()]


def test_reset_password_requires_admin_and_known_user(admin, user):
    uid = user.get("/auth/me").json()["id"]
    assert user.post(f"/admin/users/{uid}/reset-password").status_code == 403
    assert admin.post("/admin/users/999/reset-password").status_code == 404


def test_change_password_rules(user, client):
    assert client.post("/auth/change-password",
                       json={"current_password": "x", "new_password": "long-enough-pw"}).status_code == 401
    r = user.post("/auth/change-password", json={"current_password": "not-the-password", "new_password": "long-enough-pw"})
    assert r.status_code == 400 and r.json()["detail"].startswith("Your current password is wrong.")
    r = user.post("/auth/change-password", json={"current_password": DEFAULT_PASSWORD, "new_password": "short"})
    assert r.status_code == 422  # same 8+ rule as register
    r = user.post("/auth/change-password", json={"current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD})
    assert r.status_code == 422 and "different" in r.json()["detail"]


# --- CLI ---

def _run(argv, handler, monkeypatch, capsys):
    monkeypatch.setenv("PICOPAAS_TOKEN", "pp_sk_test")
    monkeypatch.setenv("PICOPAAS_URL", "https://pp.test")
    code = picopaas.main(argv, transport=httpx.MockTransport(handler))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_apps_empty_and_table(monkeypatch, capsys):
    code, out, _ = _run(["apps"], lambda r: httpx.Response(200, json=[]), monkeypatch, capsys)
    assert code == 0 and out.strip() == "No apps yet. Create one with: picopaas apply -f app.yaml"
    apps = [{"id": 1, "name": "demo", "hostname": "a.x.y", "status": "error", "image": "i"},
            {"id": 2, "name": "much-longer", "hostname": "b.x.y", "status": "running", "image": "i"}]
    code, out, _ = _run(["apps"], lambda r: httpx.Response(200, json=apps), monkeypatch, capsys)
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "URL", "STATUS"]
    assert lines[1].split() == ["demo", "https://a.x.y", "failed"]
    assert lines[1].index("https") == lines[2].index("https")  # aligned


def test_cli_status_shows_detail_and_retry(monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(minutes=4, seconds=30)).isoformat()
    app = {"id": 1, "name": "demo", "hostname": "a.x.y", "status": "error", "image": "img:1",
           "status_detail": "Can't pull the image.", "retry_at": soon,
           "health_check_enabled": True, "health_check_path": "/healthz"}
    code, out, _ = _run(["status", "demo"], lambda r: httpx.Response(200, json=[app]), monkeypatch, capsys)
    assert code == 0
    assert "url:      https://a.x.y" in out and "status:   failed" in out
    assert "detail:   Can't pull the image." in out and "retry:    in about 5 min" in out
    assert "health:   /healthz" in out


def test_cli_apply_next_steps(tmp_path, monkeypatch, capsys):
    f = tmp_path / "app.yaml"
    f.write_text(SPEC)
    resp = {"created": True, "name": "demo", "hostname": "a.x.y", "status": "pending"}
    code, out, _ = _run(["apply", "-f", str(f)], lambda r: httpx.Response(200, json=resp), monkeypatch, capsys)
    assert code == 0
    assert "Created demo (queued). It'll be live in about a minute at https://a.x.y" in out
    assert "Check progress: picopaas status demo" in out
    resp = {**resp, "created": False, "status": "error", "status_detail": "Image runs as root."}
    code, out, _ = _run(["apply", "-f", str(f)], lambda r: httpx.Response(200, json=resp), monkeypatch, capsys)
    assert "Updated demo, but it failed: Image runs as root." in out


def test_cli_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PICOPAAS_TOKEN", raising=False)
    monkeypatch.setenv("PICOPAAS_URL", "https://pp.test")
    assert picopaas.main(["apps"]) == 1
    err = capsys.readouterr().err
    assert "https://pp.test/keys" in err and "export PICOPAAS_TOKEN=pp_sk_..." in err

    def boom(req):
        raise httpx.ConnectError("name or service not known")

    code, _, err = _run(["apps"], boom, monkeypatch, capsys)
    assert code == 1 and "Can't reach https://pp.test (ConnectError: name or service not known)" in err
    assert "PICOPAAS_URL" in err

    def slow(req):
        raise httpx.ReadTimeout("t")

    assert "Timed out after 30s" in _run(["apps"], slow, monkeypatch, capsys)[2]

    code, _, err = _run(["apps"], lambda r: httpx.Response(401, json={"detail": "x"}), monkeypatch, capsys)
    assert "Your PICOPAAS_TOKEN is missing, wrong or revoked" in err and "https://pp.test/keys" in err

    code, _, err = _run(["apps"], lambda r: httpx.Response(403, json={"detail": "Your account is suspended."}),
                        monkeypatch, capsys)
    assert err.strip() == "error: Your account is suspended."
    code, _, err = _run(["-v", "apps"], lambda r: httpx.Response(403, json={"detail": "Nope."}), monkeypatch, capsys)
    assert err.strip() == "error: 403: Nope."
    code, _, err = _run(["apps"], lambda r: httpx.Response(500, json={"detail": "boom."}), monkeypatch, capsys)
    assert "PicoPaaS had a problem (500)" in err

    assert _run(["status", "ghost"], lambda r: httpx.Response(200, json=[]), monkeypatch, capsys)[2].strip() == (
        "error: No app named ghost. Run `picopaas apps` to see your apps.")
    code, _, err = _run(["apply", "-f", str(tmp_path / "missing.yaml")], lambda r: None, monkeypatch, capsys)
    assert "Can't read" in err and "no such file or directory" in err and "Check the path passed to -f" in err
    f = tmp_path / "a.yaml"
    f.write_text(SPEC)
    code, _, err = _run(["apply", "-f", str(f), "--secret", "foo"], lambda r: None, monkeypatch, capsys)
    assert '--secret needs NAME=value, for example --secret API_KEY=abc123 (got "foo").' in err


def test_cli_rotate_message_and_help(monkeypatch, capsys):
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=[{"id": 3, "name": "demo", "hostname": "h", "status": "running", "image": "i"}])
        return httpx.Response(200, json={})

    code, out, _ = _run(["rotate", "demo", "--db"], handler, monkeypatch, capsys)
    assert out.strip() == "Rotated the database password for demo. The app is restarting."
    code, out, _ = _run(["rotate", "demo", "--storage"], handler, monkeypatch, capsys)
    assert "storage bucket keys" in out
    helps = []
    for sub in ("apply", "rotate"):
        with pytest.raises(SystemExit):
            picopaas.main([sub, "-h"])
        helps.append(capsys.readouterr().out)
    assert "path to the YAML spec" in helps[0] and "repeatable" in helps[0]
    assert "rotate the database password" in helps[1] and "rotate the storage bucket keys" in helps[1]
    with pytest.raises(SystemExit):
        picopaas.main(["-h"])
    top = capsys.readouterr().out
    assert "PICOPAAS_TOKEN" in top and "PICOPAAS_URL" in top
