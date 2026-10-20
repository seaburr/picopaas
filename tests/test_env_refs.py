"""app/env_refs.py::resolve — substituting `${{ ref }}` placeholders with real values."""
import pytest

from app.env_refs import EnvValidationError, resolve


def test_resolve_substitutes_known_refs():
    env = {"DATABASE_URL": "${{ db.url }}", "LOG_LEVEL": "info"}
    refs = {"db.url": "mysql://app_1:pw@mysql:3306/app_1"}
    resolved = resolve(env, refs)
    assert resolved == {"DATABASE_URL": "mysql://app_1:pw@mysql:3306/app_1", "LOG_LEVEL": "info"}


def test_resolve_leaves_literals_untouched():
    env = {"FOO": "bar", "BAZ": "${{ storage.bucket }}"}
    refs = {"storage.bucket": "pp-7"}
    resolved = resolve(env, refs)
    assert resolved["FOO"] == "bar"
    assert resolved["BAZ"] == "pp-7"


def test_resolve_unknown_ref_raises():
    env = {"DATABASE_URL": "${{ db.url }}"}
    with pytest.raises(EnvValidationError):
        resolve(env, refs={})


def test_resolve_empty_env_returns_empty():
    assert resolve({}, {"db.url": "x"}) == {}


# --- reference validation through the API (pentest #16) ---

from tests.conftest import register_and_login  # noqa: E402

BAD_VALUES = [
    "${{ app-33.db.url }}", "${{ db.URL }}", "${{ DB.url }}", "${{ nope }}", "${{}}",
    "prefix ${{ db.url }}", "${{ db.url }} suffix", "x${{",
]


def _body(value, **kw):
    b = {"name": "refapp", "image": "ghcr.io/example/demo:1.0", "port": 8080,
         "want_db": True, "want_storage": True, "env": [{"key": "X", "value": value}]}
    b.update(kw)
    return b


@pytest.mark.parametrize("value", BAD_VALUES)
def test_lookalike_refs_rejected_on_create_and_patch(client, value):
    register_and_login(client, "refs@example.com")
    r = client.post("/apps", json=_body(value))
    assert r.status_code == 422 and "Allowed" in r.text
    ok = client.post("/apps", json=_body("${{db.url}}"))
    assert ok.status_code == 201
    r = client.patch(f"/apps/{ok.json()['id']}", json={"env": [{"key": "X", "value": value}]})
    assert r.status_code == 422


@pytest.mark.parametrize("value", ["${{db.url}}", "${{ db.url }}", "${{  storage.bucket  }}", "plain"])
def test_valid_refs_accepted(client, value):
    register_and_login(client, "refs2@example.com")
    assert client.post("/apps", json=_body(value)).status_code == 201


def test_ref_needs_enabled_resource(client):
    register_and_login(client, "refs3@example.com")
    assert client.post("/apps", json=_body("${{ db.url }}", want_db=False)).status_code == 422
    assert client.post("/apps", json=_body("${{ storage.bucket }}", want_storage=False)).status_code == 422


def test_spec_rejects_bad_ref(client):
    register_and_login(client, "refs4@example.com")
    spec = ("name: refspec\nimage: ghcr.io/example/demo:1.0\nport: 8080\n"
            "env:\n  X: \"${{ app-33.db.url }}\"\n")
    r = client.post("/apps/spec", json={"spec": spec})
    assert r.status_code == 422


def test_app_url_ref_allowed_and_resolves():
    from app import config, env_refs, models
    from app.reconciler import _app_url
    assert "app.url" in env_refs.ALLOWED_REFS
    env_refs.validate_env_value("${{ app.url }}", False, False)
    url = _app_url(models.App(hostname="myapp-ab12cd"))
    assert url == f"https://myapp-ab12cd.{config.APP_DOMAIN}"
    assert env_refs.resolve({"SITE_URL": "${{ app.url }}"}, {"app.url": url}) == {"SITE_URL": url}
