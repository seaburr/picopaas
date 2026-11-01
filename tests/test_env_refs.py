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
    "x${{", "${{ db.url }", "${{ db.url", "${{ ${{ db.url }}", "${{{ db.url }}}", "a ${{ db.url }} ${{ nope }}",
    "${{ db.url }} ${{ DB.url }}", "a ${{ db.url }} and ${{",
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


@pytest.mark.parametrize("value", [
    "${{db.url}}", "${{ db.url }}", "${{  storage.bucket  }}", "plain", "prefix ${{ db.url }}", "${{ db.url }} suffix",
    "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslrootcert=${{ db.ca_file }}",
    "${{ db.host }}${{ db.port }}",
])
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


def test_app_url_prefers_reserved_subdomain_then_active_custom_domain():
    """${{ app.url }} resolves to an app's nicest host: active custom domain > reserved subdomain > generated."""
    from app import config, models
    from app.reconciler import _app_url

    base = dict(hostname="myapp-ab12cd")
    # No nicer host set -> generated host.
    assert _app_url(models.App(**base)) == f"https://myapp-ab12cd.{config.APP_DOMAIN}"
    # Reserved platform subdomain wins over the generated host.
    assert _app_url(models.App(**base, platform_subdomain="blog")) == f"https://blog.{config.APP_DOMAIN}"
    # A PENDING custom domain does not win (it isn't being served yet) -> falls back to the subdomain.
    assert _app_url(models.App(**base, platform_subdomain="blog",
                               custom_domain="www.example.com", custom_domain_status="pending")) \
        == f"https://blog.{config.APP_DOMAIN}"
    # An ACTIVE custom domain is the most-preferred host.
    assert _app_url(models.App(**base, platform_subdomain="blog",
                               custom_domain="www.example.com", custom_domain_status="active")) \
        == "https://www.example.com"


# --- interpolation: refs inside a longer value ---

from app import config, env_refs  # noqa: E402

DSN = "postgres://${{ db.user }}:${{ db.password }}@${{ db.host }}:${{ db.port }}/${{ db.name }}?sslrootcert=${{ db.ca_file }}"
REFS = {"db.user": "u1", "db.password": "pw1", "db.host": "h.internal", "db.port": "25060", "db.name": "d1",
        "db.ca_file": "/etc/picopaas/db-ca.pem"}


def test_resolve_mixed_value_with_several_refs():
    out = resolve({"DSN": DSN, "WHOLE": "  ${{ db.port }}  ", "HOSTPORT": "${{db.host}}:${{ db.port }}"}, REFS)
    assert out["DSN"] == "postgres://u1:pw1@h.internal:25060/d1?sslrootcert=/etc/picopaas/db-ca.pem"
    assert out["WHOLE"] == "25060"  # a whole-value ref behaves as before (surrounding whitespace ignored)
    assert out["HOSTPORT"] == "h.internal:25060"


def test_resolve_is_single_pass_never_recursive():
    refs = {"db.password": "${{ db.user }}", "db.user": "SHOULD-NOT-APPEAR"}
    assert resolve({"X": "a-${{ db.password }}-b"}, refs) == {"X": "a-${{ db.user }}-b"}
    assert resolve({"X": "${{ db.password }}"}, refs) == {"X": "${{ db.user }}"}


def test_resolve_unknown_ref_inside_longer_value_raises():
    with pytest.raises(EnvValidationError):
        resolve({"X": "a ${{ db.url }} b"}, refs={})


def test_validate_mixed_value_availability_rules():
    env_refs.validate_env_value(DSN, True, False)
    with pytest.raises(EnvValidationError, match="needs a database"):
        env_refs.validate_env_value("x ${{ db.url }}", False, True)
    with pytest.raises(EnvValidationError, match="needs a storage bucket"):
        env_refs.validate_env_value("x ${{ app.url }} y ${{ storage.bucket }}", True, False)
    with pytest.raises(EnvValidationError, match="Unknown reference"):
        env_refs.validate_env_value("ok ${{ db.nope }}", True, True)


def test_validate_ref_count_cap():
    ok = "${{ app.url }}" * config.ENV_MAX_REFS_PER_VALUE
    env_refs.validate_env_value(ok, False, False)
    with pytest.raises(EnvValidationError, match=str(config.ENV_MAX_REFS_PER_VALUE)):
        env_refs.validate_env_value(ok + "${{ app.url }}", False, False)


def test_mixed_value_stored_as_ref_text_and_422_through_api(client):
    register_and_login(client, "refs5@example.com")
    r = client.post("/apps", json=_body("x ${{ db.url }} y ${{ db.nope }}"))
    assert r.status_code == 422 and "Unknown reference" in r.text
    r = client.post("/apps", json=_body("jdbc:${{ db.host }}/${{ db.name }}", want_db=False))
    assert r.status_code == 422 and "needs a database" in r.text
    r = client.post("/apps", json=_body("jdbc:${{ db.host }}/${{ db.name }}"))
    assert r.status_code == 201
    env = client.get(f"/apps/{r.json()['id']}").json()["env"]
    assert [e["value"] for e in env if e["key"] == "X"] == ["jdbc:${{ db.host }}/${{ db.name }}"]


def test_env_value_size_cap():
    """PENTEST K56 (Chas 2026-11-01): one stored env value is at most 8 KiB (UTF-8 bytes, before refs resolve)."""
    import pytest
    from pydantic import ValidationError

    from app import config
    from app.schemas import EnvVarIn

    assert config.ENV_VALUE_MAX_BYTES == 8192
    EnvVarIn(key="BIG", value="a" * 8192)
    with pytest.raises(ValidationError, match="at most 8 KiB"):
        EnvVarIn(key="BIG", value="a" * 8193)
    with pytest.raises(ValidationError):
        EnvVarIn(key="BIG", value="é" * 4097)  # 8194 bytes in UTF-8


def test_env_value_size_cap_api(client):
    from tests.conftest import register_and_login

    register_and_login(client, "bigenv@example.com")
    r = client.post("/apps", json={"name": "big", "image": "nginxinc/nginx-unprivileged:alpine", "port": 8080,
                                   "env": [{"key": "BIG", "value": "x" * 9000}]})
    assert r.status_code == 422 and "8 KiB" in r.text
