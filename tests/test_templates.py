import re

from app.env_refs import ALLOWED_REFS, ENV_KEY_RE, REF_ANY_RE, validate_env_value
from app.routers.templates import CATALOG
from tests.conftest import register_and_login

EXPECTED = {"ghost", "wordpress", "directus", "grafana", "grafana-pg", "n8n", "shlink", "miniflux", "forgejo", "memos",
            "vaultwarden", "wikijs", "status",
            "static-site"}


def test_templates_catalog(client):
    assert client.get("/templates").status_code == 401
    register_and_login(client, "tpl@example.com")
    r = client.get("/templates")
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.json()}
    assert set(by_id) == EXPECTED  # podinfo is gone
    g = by_id["ghost"]
    assert g["port"] == 2368 and g["wants_db"] and g["wants_storage"]
    assert g["env"]["SITE_URL"] == "${{ app.url }}"
    for t in by_id.values():
        for k in ("name", "description", "image", "port", "wants_db", "db_engine", "wants_storage", "env", "secrets", "notes"):
            assert k in t


def test_wordpress_template(client):
    register_and_login(client, "wp@example.com")
    wp = {t["id"]: t for t in client.get("/templates").json()}["wordpress"]
    assert wp["volume"] == {"size_gi": 1, "mount_path": "/var/www/html", "required": False}
    assert wp["health_check"]["path"] == "/healthz"
    assert len(wp["secrets"]) == 8 and all(s["key"].startswith("WORDPRESS_") for s in wp["secrets"])
    assert "WP_ADMIN_EMAIL" in wp["env"] and wp["env"]["WP_URL"] == "${{ app.url }}"


def test_n8n_uses_postgres(client):
    register_and_login(client, "n8n@example.com")
    t = {x["id"]: x for x in client.get("/templates").json()}["n8n"]
    assert t["db_engine"] == "pg" and t["wants_db"]
    assert [s["key"] for s in t["secrets"]] == ["N8N_ENCRYPTION_KEY"]
    # verified live: mount the whole home so both ~/.n8n and ~/.cache are on the volume
    assert t["volume"] == {"size_gi": 1, "mount_path": "/home/node", "required": True}
    assert t["env"]["N8N_USER_FOLDER"] == "/home/node"


def test_catalog_is_consistent():
    ids = [t.id for t in CATALOG]
    assert len(ids) == len(set(ids))
    for t in CATALOG:
        keys = list(t.env) + [s.key for s in t.secrets]
        assert len(keys) == len(set(keys)), t.id
        for k in keys:
            assert ENV_KEY_RE.match(k), (t.id, k)
        for k, v in t.env.items():
            # Same rules as the API: every ${{ }} is an allowed ref, available for the template's add-ons.
            validate_env_value(v, want_db=t.wants_db, want_storage=t.wants_storage)
            for ref in (m.group(1) for m in REF_ANY_RE.finditer(v)):
                assert ref in ALLOWED_REFS, (t.id, k, v)
        assert 1024 <= t.port <= 65535
        assert t.image and not t.image.startswith("http")
        if t.docs_url:
            assert re.match(r"^https://github\.com/seaburr/picopaas/tree/main/examples/[a-z0-9-]+$", t.docs_url)
        if t.volume:
            assert t.volume.mount_path.startswith("/") and t.volume.size_gi >= 1
        if t.health_check:
            assert t.health_check.path.startswith("/")
        for s in t.secrets:
            assert s.hint


def test_template_env_is_accepted_by_create_app(client):
    """Every catalog entry, prefilled the way the form does it, must be a valid POST /apps body shape."""
    from app.schemas import EnvVarIn

    for t in CATALOG:
        for k, v in t.env.items():
            EnvVarIn(key=k, value=v)
        for s in t.secrets:
            EnvVarIn(key=s.key, value="", is_secret=True)


def test_prefilled_template_with_empty_secrets_creates_app(client):
    register_and_login(client, "wpc@example.com")
    t = {x["id"]: x for x in client.get("/templates").json()}["shlink"]
    env = [{"key": k, "value": v, "is_secret": False} for k, v in t["env"].items()]
    env += [{"key": s["key"], "value": "", "is_secret": True} for s in t["secrets"]]
    r = client.post("/apps", json={"name": "shlink", "image": t["image"], "port": t["port"], "want_db": True,
                                   "db_engine": t["db_engine"], "env": env})
    assert r.status_code == 201, r.text


def test_grafana_pg_variant(client):
    register_and_login(client, "gpg@example.com")
    t = {x["id"]: x for x in client.get("/templates").json()}["grafana-pg"]
    assert t["db_engine"] == "pg" and t["env"]["GF_DATABASE_TYPE"] == "postgres"
    assert t["env"]["GF_DATABASE_SSL_MODE"] == "require" and t["env"]["GF_DATABASE_URL"] == "${{ db.url }}"


def test_miniflux_and_forgejo_templates(client):
    """Both verified live 2026-11-01 with the public images unchanged."""
    register_and_login(client, "rss@example.com")
    by_id = {x["id"]: x for x in client.get("/templates").json()}
    m = by_id["miniflux"]
    assert m["db_engine"] == "pg" and "sslmode=verify-full&sslrootcert=${{ db.ca_file }}" in m["env"]["DATABASE_URL"]
    assert [s["key"] for s in m["secrets"]] == ["ADMIN_PASSWORD"]
    f = by_id["forgejo"]
    assert not f["wants_db"] and f["image"].endswith("-rootless")
    assert f["volume"] == {"size_gi": 1, "mount_path": "/var/lib/gitea", "required": True}


def test_memos_vaultwarden_wikijs_templates(client):
    """Verified live 2026-11-01. Wiki.js needs the CA body ref (DB_SSL_CA) and a volume for /wiki/data."""
    register_and_login(client, "wiki@example.com")
    by_id = {x["id"]: x for x in client.get("/templates").json()}
    assert by_id["memos"]["env"]["MEMOS_DSN"] == by_id["miniflux"]["env"]["DATABASE_URL"] and by_id["memos"]["db_engine"] == "pg"
    assert by_id["vaultwarden"]["volume"] == {"size_gi": 1, "mount_path": "/data", "required": True}
    w = by_id["wikijs"]
    assert w["env"]["DB_SSL_CA"] == "${{ db.ca_cert_body }}" and w["volume"]["mount_path"] == "/wiki/data"
