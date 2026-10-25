import re

from tests.conftest import register_and_login

HOSTNAME_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z0-9]{7}\.picopaas\.xyz$")


def _create_app(client, **overrides):
    payload = {
        "name": "demo",
        "image": "ghcr.io/example/demo:1.0",
        "port": 8080,
        "want_db": False,
        "want_storage": False,
        "env": [],
    }
    payload.update(overrides)
    return client.post("/apps", json=payload)


def test_app_limit_enforced_for_regular_user(make_client):
    # Register an unrelated first user so the one under test is NOT the
    # first-user-becomes-platform_admin case, and is subject to app_limit.
    register_and_login(make_client(), "admin-seed@example.com")

    client = make_client()
    register_and_login(client, "limited@example.com")

    first = _create_app(client, name="app-one")
    assert first.status_code == 201

    second = _create_app(client, name="app-two")
    assert second.status_code == 403


def test_platform_admin_bounded_by_hard_max(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "MAX_APPS_PER_USER", 1)  # the user pair doesn't apply to admins
    monkeypatch.setattr(config, "ADMIN_DEFAULT_APP_LIMIT", 3)
    monkeypatch.setattr(config, "MAX_APPS_PER_ADMIN", 3)
    register_and_login(client, "admin@example.com")  # first user => platform_admin (app_limit stays 1)

    for i in range(3):
        resp = _create_app(client, name=f"admin-app-{i}")
        assert resp.status_code == 201, resp.text
    blocked = _create_app(client, name="admin-app-x")
    assert blocked.status_code == 403
    assert "limit of 3 apps" in blocked.json()["detail"]


def test_effective_limit_is_min_of_app_limit_and_hard_max(make_client, db_session):
    from app import models
    register_and_login(make_client(), "admin-seed@example.com")
    for limit, cap in ((3, 3), (10, 5)):
        client = make_client()
        email = f"u{limit}@example.com"
        register_and_login(client, email)
        db_session.query(models.User).filter_by(email=email).one().app_limit = limit
        db_session.commit()
        codes = [_create_app(client, name=f"a{i}").status_code for i in range(cap + 1)]
        assert codes == [201] * cap + [403], (limit, codes)


def test_patch_app_limit_clamped_to_hard_max(make_client):
    from app import config
    from tests.conftest import register_and_login as rl
    admin = make_client()
    rl(admin, "admin-seed@example.com")
    user = make_client()
    uid = rl(user, "target@example.com").json()["id"]
    over = admin.patch(f"/admin/users/{uid}", json={"app_limit": config.MAX_APPS_PER_USER + 1})
    assert over.status_code == 422
    ok = admin.patch(f"/admin/users/{uid}", json={"app_limit": config.MAX_APPS_PER_USER})
    assert ok.status_code == 200
    assert ok.json()["app_limit_effective"] == config.MAX_APPS_PER_USER


def test_me_exposes_max_and_effective_limit(make_client):
    from app import config
    admin = make_client()
    body = register_and_login(admin, "admin-seed@example.com").json()
    me = admin.get("/auth/me").json()
    assert me["max_apps"] == config.MAX_APPS_PER_ADMIN
    assert me["app_limit_effective"] == config.ADMIN_DEFAULT_APP_LIMIT  # admin: admin default, not stale app_limit=1
    user = make_client()
    register_and_login(user, "plain@example.com")
    me = user.get("/auth/me").json()
    assert me["app_limit"] == 1 and me["app_limit_effective"] == 1


def test_cross_user_isolation_returns_404(make_client):
    client_a = make_client()
    register_and_login(client_a, "owner@example.com")
    created = _create_app(client_a, name="owners-app")
    assert created.status_code == 201
    app_id = created.json()["id"]

    client_b = make_client()
    register_and_login(client_b, "intruder@example.com")

    resp = client_b.get(f"/apps/{app_id}")
    assert resp.status_code == 404

    resp = client_b.patch(f"/apps/{app_id}", json={"name": "hijacked"})
    assert resp.status_code == 404

    resp = client_b.delete(f"/apps/{app_id}")
    assert resp.status_code == 404

    # The owner can still see it fine.
    assert client_a.get(f"/apps/{app_id}").status_code == 200


def test_hostname_format(client):
    register_and_login(client, "hostnames@example.com")
    resp = _create_app(client, name="format-check")
    assert resp.status_code == 201
    hostname = resp.json()["hostname"]
    assert HOSTNAME_RE.match(hostname), hostname


def test_secret_env_var_is_masked_but_plain_is_visible(client):
    register_and_login(client, "secrets@example.com")
    resp = _create_app(
        client,
        name="with-secrets",
        env=[
            {"key": "LOG_LEVEL", "value": "info", "is_secret": False},
            {"key": "API_TOKEN", "value": "super-secret-value", "is_secret": True},
        ],
    )
    assert resp.status_code == 201
    env = {e["key"]: e for e in resp.json()["env"]}

    assert env["LOG_LEVEL"]["value"] == "info"
    assert env["API_TOKEN"]["is_secret"] is True
    assert env["API_TOKEN"]["value"] == "********"
    assert "super-secret-value" not in resp.text

    # Masking also holds on a subsequent fetch, not just the create response.
    app_id = resp.json()["id"]
    fetched = client.get(f"/apps/{app_id}")
    assert fetched.status_code == 200
    assert "super-secret-value" not in fetched.text


def test_bad_env_key_is_rejected(client):
    register_and_login(client, "badkey@example.com")
    resp = _create_app(
        client,
        name="bad-key-app",
        env=[{"key": "not-a-valid-key", "value": "x", "is_secret": False}],
    )
    assert resp.status_code == 422


def test_db_ref_rejected_without_want_db(client):
    register_and_login(client, "badref@example.com")
    resp = _create_app(
        client,
        name="bad-ref-app",
        want_db=False,
        env=[{"key": "DATABASE_URL", "value": "${{ db.url }}", "is_secret": False}],
    )
    assert resp.status_code == 422


def test_db_ref_accepted_with_want_db(client):
    register_and_login(client, "goodref@example.com")
    resp = _create_app(
        client,
        name="good-ref-app",
        want_db=True,
        env=[{"key": "DATABASE_URL", "value": "${{ db.url }}", "is_secret": False}],
    )
    assert resp.status_code == 201
    env = {e["key"]: e for e in resp.json()["env"]}
    assert env["DATABASE_URL"]["value"] == "${{ db.url }}"


def test_unknown_ref_rejected(client):
    register_and_login(client, "unknownref@example.com")
    resp = _create_app(
        client,
        name="unknown-ref-app",
        want_db=True,
        env=[{"key": "FOO", "value": "${{ db.nope }}", "is_secret": False}],
    )
    assert resp.status_code == 422


def test_apps_start_pending_with_no_k8s_calls(client):
    register_and_login(client, "pending@example.com")
    resp = _create_app(client, name="pending-app")
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"


def test_resending_masked_secret_keeps_stored_value(client, db_session):
    from app import crypto, models

    register_and_login(client, "keepsecret@example.com")
    app_id = _create_app(
        client,
        name="keep",
        env=[{"key": "API_TOKEN", "value": "original-secret", "is_secret": True}],
    ).json()["id"]

    # The UI round-trips the masked value it was shown, plus a changed plain var.
    resp = client.patch(
        f"/apps/{app_id}",
        json={"env": [
            {"key": "API_TOKEN", "value": "********", "is_secret": True},
            {"key": "LOG_LEVEL", "value": "debug", "is_secret": False},
        ]},
    )
    assert resp.status_code == 200

    stored = db_session.query(models.AppEnvVar).filter_by(app_id=app_id, key="API_TOKEN").one()
    assert crypto.decrypt(stored.value_encrypted) == "original-secret"

    # An explicitly new secret value does replace it.
    client.patch(f"/apps/{app_id}", json={"env": [{"key": "API_TOKEN", "value": "rotated", "is_secret": True}]})
    db_session.expire_all()
    stored = db_session.query(models.AppEnvVar).filter_by(app_id=app_id, key="API_TOKEN").one()
    assert crypto.decrypt(stored.value_encrypted) == "rotated"


def test_status_detail_is_exposed(client, db_session):
    from app import models

    register_and_login(client, "detail@example.com")
    app_id = _create_app(client, name="detail").json()["id"]
    row = db_session.get(models.App, app_id)
    row.status, row.status_detail = "error", "ImagePullBackOff"
    db_session.commit()
    body = client.get(f"/apps/{app_id}").json()
    assert body["status"] == "error" and body["status_detail"] == "ImagePullBackOff"
