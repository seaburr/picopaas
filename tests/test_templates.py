from tests.conftest import register_and_login


def test_templates_catalog(client):
    assert client.get("/templates").status_code == 401
    register_and_login(client, "tpl@example.com")
    r = client.get("/templates")
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.json()}
    assert set(by_id) == {"ghost", "static-site", "podinfo"}
    g = by_id["ghost"]
    assert g["port"] == 2368 and g["wants_db"] and g["wants_storage"]
    assert g["env"]["SITE_URL"] == "${{ app.url }}"
    for t in by_id.values():
        for k in ("name", "description", "image", "port", "wants_db", "wants_storage", "env", "notes"):
            assert k in t
