import httpx
import pytest

from app import models
from cli import picopaas
from tests.conftest import register_and_login

SPEC = """\
apiVersion: picopaas/v1
name: demo
image: ghcr.io/example/demo:1.0
port: 8080
resources: {db: true, storage: false}
env:
  LOG_LEVEL: info
  DATABASE_URL: ${{ db.url }}
secrets:
  - API_TOKEN
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


def test_admin_authz(admin, user, client):
    assert user.get("/admin/users").status_code == 403
    assert user.get("/admin/db-servers").status_code == 403
    assert user.patch("/admin/users/1", json={"app_limit": 5}).status_code == 403
    assert client.get("/admin/users").status_code == 401
    users = admin.get("/admin/users").json()
    assert [u["email"] for u in users] == ["admin@example.com", "user@example.com"]
    assert users[1]["app_count"] == 0 and users[1]["app_limit"] == 1
    assert "password_hash" not in users[0]


def test_app_limit_change_takes_effect(admin, user):
    body = {"name": "a", "image": "x/y:1", "port": 80}
    assert user.post("/apps", json=body).status_code == 201
    assert user.post("/apps", json={**body, "name": "b"}).status_code == 403
    uid = [u for u in admin.get("/admin/users").json() if u["role"] == "user"][0]["id"]
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": -1}).status_code == 422
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 2, "role": "uberadmin"}).status_code == 422
    assert admin.patch(f"/admin/users/{uid}", json={"app_limit": 2}).json()["role"] == "user"
    assert user.post("/apps", json={**body, "name": "b"}).status_code == 201


def test_db_servers_math_and_no_creds(admin, db_session):
    r = admin.post("/admin/db-servers", json={
        "name": "s1", "host": "h", "port": 3306, "admin_user": "root",
        "admin_password": "hunter2-secret", "max_databases": 5})
    assert r.status_code == 201
    assert "hunter2" not in r.text and "admin_user" not in r.json()
    sid = r.json()["id"]
    stored = db_session.get(models.DbServer, sid)
    assert stored.admin_password_encrypted != "hunter2-secret"
    assert admin.post("/admin/db-servers", json={
        "name": "s1", "host": "h", "admin_user": "r", "admin_password": "p", "max_databases": 1}).status_code == 409
    owner = db_session.query(models.User).first()
    for i in range(4):
        a = models.App(owner_id=owner.id, name=f"n{i}", hostname=f"h{i}", image="i", port=1)
        db_session.add(a)
        db_session.flush()
        db_session.add(models.AppDatabase(app_id=a.id, db_server_id=sid, db_name=f"d{i}",
                                          db_user=f"u{i}", password_encrypted="x"))
    db_session.commit()
    s = admin.get("/admin/db-servers").json()[0]
    assert (s["used"], s["pct"], s["warn"]) == (4, 80.0, True)
    s = admin.patch(f"/admin/db-servers/{sid}", json={"max_databases": 10, "status": "draining"}).json()
    assert (s["pct"], s["warn"], s["status"]) == (40.0, False, "draining")
    assert admin.patch(f"/admin/db-servers/{sid}", json={"status": "bogus"}).status_code == 422
    assert "hunter2" not in admin.get("/admin/db-servers").text


def test_spec_create_update_secrets(user):
    r = user.post("/apps/spec", json={"spec": SPEC, "secrets": {"API_TOKEN": "t1"}})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["created"] is True and j["want_db"] is True
    env = {e["key"]: e for e in j["env"]}
    assert env["API_TOKEN"]["value"] == "********" and env["LOG_LEVEL"]["value"] == "info"
    # update without the secret keeps it; changes apply
    r2 = user.post("/apps/spec", json={"spec": SPEC.replace("8080", "9090"), "secrets": {}})
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert r2.json()["id"] == j["id"] and r2.json()["port"] == 9090
    assert any(e["key"] == "API_TOKEN" for e in r2.json()["env"])


def test_spec_secret_kept_in_db(user, db_session):
    user.post("/apps/spec", json={"spec": SPEC, "secrets": {"API_TOKEN": "t1"}})
    user.post("/apps/spec", json={"spec": SPEC, "secrets": {}})
    from app import crypto
    row = db_session.query(models.AppEnvVar).filter_by(key="API_TOKEN").one()
    assert crypto.decrypt(row.value_encrypted) == "t1"
    user.post("/apps/spec", json={"spec": SPEC, "secrets": {"API_TOKEN": "t2"}})
    db_session.expire_all()
    row = db_session.query(models.AppEnvVar).filter_by(key="API_TOKEN").one()
    assert crypto.decrypt(row.value_encrypted) == "t2"


def test_spec_validation(user, admin):
    def post(spec, secrets=None):
        return user.post("/apps/spec", json={"spec": spec, "secrets": secrets or {}})
    assert post(SPEC).status_code == 422  # missing secret on create
    assert post(SPEC + "bogus: 1\n", {"API_TOKEN": "x"}).status_code == 422
    assert post(SPEC, {"API_TOKEN": "x", "OTHER": "y"}).status_code == 422
    assert post(SPEC.replace("picopaas/v1", "v2"), {"API_TOKEN": "x"}).status_code == 422
    assert post(SPEC.replace("LOG_LEVEL: info", "LOG_LEVEL: 1"), {"API_TOKEN": "x"}).status_code == 422
    assert post(SPEC.replace("${{ db.url }}", "${{ nope.url }}"), {"API_TOKEN": "x"}).status_code == 422
    assert post("- a\n- b\n").status_code == 422
    assert post("a: [unclosed").status_code == 422
    assert post(SPEC.replace("db: true", "db: false"), {"API_TOKEN": "x"}).status_code == 422  # ref w/o resource
    # limit: user already has 0; create one, second name is refused
    assert post(SPEC, {"API_TOKEN": "x"}).status_code == 200
    assert post(SPEC.replace("name: demo", "name: other"), {"API_TOKEN": "x"}).status_code == 403


def test_spec_yaml_bombs_and_size(user):
    bomb = "a: &a [x, x]\nb: &b [*a, *a]\nc: &c [*b, *b]\nd: *c\n"
    r = user.post("/apps/spec", json={"spec": bomb})
    assert r.status_code == 422 and "alias" in r.json()["detail"]
    assert user.post("/apps/spec", json={"spec": "# " + "x" * 70000}).status_code == 422
    assert user.post("/apps/spec", json={"spec": "[" * 5000 + "]" * 5000}).status_code == 422
    assert user.post("/apps/spec", json={"spec": "!!python/object/apply:os.system ['id']"}).status_code == 422


def test_cli_parsing_and_flow(tmp_path, monkeypatch, capsys):
    p = picopaas.build_parser()
    a = p.parse_args(["apply", "-f", "s.yaml", "--secret", "A=1", "--secret", "B=x=y"])
    assert a.secret == ["A=1", "B=x=y"]
    with pytest.raises(SystemExit):
        p.parse_args(["rotate", "x"])
    with pytest.raises(SystemExit):
        p.parse_args(["rotate", "x", "--db", "--storage"])

    f = tmp_path / "s.yaml"
    f.write_text(SPEC)
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/apps/spec":
            return httpx.Response(200, json={"created": True, "name": "demo", "hostname": "a-b-12345.raisehellpraisedale.xyz", "status": "pending"})
        if req.url.path == "/apps":
            return httpx.Response(200, json=[{"id": 7, "name": "demo", "hostname": "h", "status": "running", "image": "i"}])
        return httpx.Response(422, json={"detail": "nope"})

    t = httpx.MockTransport(handler)
    monkeypatch.setenv("PICOPAAS_TOKEN", "pp_sk_test")
    assert picopaas.main(["apply", "-f", str(f), "--secret", "API_TOKEN=s"], transport=t) == 0
    import json
    assert json.loads(seen[0].content) == {"spec": SPEC, "secrets": {"API_TOKEN": "s"}}
    assert seen[0].headers["authorization"] == "Bearer pp_sk_test"
    assert "created demo" in capsys.readouterr().out
    assert picopaas.main(["apps"], transport=t) == 0
    assert "demo\th\trunning" in capsys.readouterr().out
    assert picopaas.main(["rotate", "demo", "--db"], transport=t) == 1
    assert "422: nope" in capsys.readouterr().err
    assert json.loads(seen[-1].content) == {"target": "db"} and seen[-1].url.path == "/apps/7/rotate-credentials"
    monkeypatch.delenv("PICOPAAS_TOKEN")
    assert picopaas.main(["apps"], transport=t) == 1
