import re

from app.env_refs import ALLOWED_REFS, ENV_KEY_RE, REF_ANY_RE, validate_env_value
from app.routers.templates import CATALOG
from tests.conftest import register_and_login

EXPECTED = {"ghost", "wordpress", "directus", "grafana", "grafana-pg", "n8n", "shlink", "miniflux", "forgejo", "memos",
            "vaultwarden", "wikijs", "status",
            "static-site", "vikunja", "umami", "ntfy", "it-tools", "excalidraw"}


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
    assert f["wants_db"] and f["db_engine"] == "mysql" and f["image"].endswith("-rootless")
    # CA-verified MySQL (verified live 2026-11-03): Go trusts the platform CA via SSL_CERT_FILE
    assert f["env"]["DB_TYPE"] == "mysql" and f["env"]["FORGEJO__DATABASE__SSL_MODE"] == "true"
    assert f["env"]["SSL_CERT_FILE"] == "${{ db.ca_file }}" and f["env"]["DB_HOST"] == "${{ db.host }}:${{ db.port }}"
    assert f["volume"] == {"size_gi": 1, "mount_path": "/var/lib/gitea", "required": True}


def test_memos_vaultwarden_wikijs_templates(client):
    """Verified live 2026-11-01. Wiki.js needs the CA body ref (DB_SSL_CA) and a volume for /wiki/data."""
    register_and_login(client, "wiki@example.com")
    by_id = {x["id"]: x for x in client.get("/templates").json()}
    assert by_id["memos"]["env"]["MEMOS_DSN"] == by_id["miniflux"]["env"]["DATABASE_URL"] and by_id["memos"]["db_engine"] == "pg"
    assert by_id["vaultwarden"]["volume"] == {"size_gi": 1, "mount_path": "/data", "required": True}
    w = by_id["wikijs"]
    assert w["env"]["DB_SSL_CA"] == "${{ db.ca_cert_body }}" and w["volume"]["mount_path"] == "/wiki/data"


def test_vikunja_umami_ntfy_itools_excalidraw_templates(client):
    """Verified live 2026-11-03 (examples/<app>/)."""
    register_and_login(client, "five@example.com")
    by_id = {x["id"]: x for x in client.get("/templates").json()}
    v = by_id["vikunja"]
    assert v["db_engine"] == "pg" and v["wants_db"] and v["wants_storage"] and v["port"] == 3456
    assert v["env"]["VIKUNJA_DATABASE_SSLROOTCERT"] == "${{ db.ca_file }}"
    assert [s["key"] for s in v["secrets"]] == ["VIKUNJA_SERVICE_SECRET"]
    u = by_id["umami"]
    assert u["db_engine"] == "pg" and not u["wants_storage"] and "sslrootcert=${{ db.ca_file }}" in u["env"]["DATABASE_URL"]
    assert [s["key"] for s in u["secrets"]] == ["APP_SECRET"] and "admin / umami" in u["notes"]
    n = by_id["ntfy"]
    assert n["env"]["NTFY_AUTH_DEFAULT_ACCESS"] == "deny-all" and "pool_max_conns=3" in n["env"]["NTFY_DATABASE_URL"]
    assert [s["key"] for s in n["secrets"]] == ["NTFY_AUTH_USERS"] and n["health_check"]["path"] == "/v1/health"
    for tid in ("it-tools", "excalidraw"):
        t = by_id[tid]
        assert not t["wants_db"] and not t["wants_storage"] and t["port"] == 8080 and t["health_check"]["path"] == "/"
        assert t["env"] == {} and t["secrets"] == []
    assert "planka" not in by_id


def test_new_pg_templates_create_app_with_empty_secrets(client, db_session):
    from app import models
    db_session.add(models.DbServer(name="pg1", engine="pg", host="h", port=5432, admin_user="u", admin_password_encrypted="x"))
    db_session.commit()
    register_and_login(client, "ntfyc@example.com")
    t = {x["id"]: x for x in client.get("/templates").json()}["ntfy"]
    env = [{"key": k, "value": v, "is_secret": False} for k, v in t["env"].items()]
    env += [{"key": s["key"], "value": "", "is_secret": True} for s in t["secrets"]]
    r = client.post("/apps", json={"name": "ntfy", "image": t["image"], "port": t["port"], "want_db": True,
                                   "db_engine": "pg", "env": env})
    assert r.status_code == 201, r.text


def test_cards_have_short_descriptions_and_upstream_docs():
    for t in CATALOG:
        assert t.upstream_docs_url and t.upstream_docs_url.startswith("https://"), t.id
        assert len(t.description) <= 140, t.id
        assert t.description.count(". ") == 0 and t.description.endswith("."), t.id
