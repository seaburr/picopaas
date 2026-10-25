"""Renaming an app must not roll the pod: the edit form re-sends every field and none of them is a change."""
from app import config, models
from app.dependencies import get_registry
from app.main import app as fastapi_app
from app.providers.fake import FakeRegistry
from tests.conftest import register_and_login


class CountingRegistry(FakeRegistry):
    def __init__(self):
        super().__init__(default_size_bytes=1024)
        self.calls = 0

    def inspect(self, *a, **kw):
        self.calls += 1
        return super().inspect(*a, **kw)

    def image_size_bytes(self, *a, **kw):
        self.calls += 1
        return super().image_size_bytes(*a, **kw)


def test_rename_with_full_payload_resent_changes_nothing(make_client, db_session, monkeypatch):
    monkeypatch.setattr(config, "VOLUMES_ENABLED", True)
    c = make_client()
    register_and_login(c, "rename@example.com")
    body = {"name": "web", "image": "ghcr.io/example/demo:1.0", "port": 8080,
            "env": [{"key": "PLAIN", "value": "x", "is_secret": False},
                    {"key": "TOKEN", "value": "not-the-password", "is_secret": True}],
            "health_check_enabled": False, "health_check_path": "/", "auto_update": True,
            "volume": {"size_gi": 1, "mount_path": "/data"}}
    r = c.post("/apps", json=body)
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    row = db_session.get(models.App, aid)
    row.status = "running"
    row.health_check_timeout_seconds = 7  # stored, while the form sends null because the check is off
    row.image_digest = "sha256:" + "a" * 64
    db_session.commit()

    reg = CountingRegistry()
    reg.digests["ghcr.io/example/demo:1.0"] = "sha256:" + "b" * 64  # the tag has moved since
    fastapi_app.dependency_overrides[get_registry] = lambda: reg
    payload = {**body, "name": "renamed", "health_check_timeout_seconds": None,
               "env": [{"key": "PLAIN", "value": "x", "is_secret": False},
                       {"key": "TOKEN", "value": "********", "is_secret": True}]}
    # use whatever mask the API actually shows
    shown = {e["key"]: e["value"] for e in c.get(f"/apps/{aid}").json()["env"]}
    payload["env"][1]["value"] = shown["TOKEN"]
    r = c.patch(f"/apps/{aid}", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "renamed"
    db_session.expire_all()
    row = db_session.get(models.App, aid)
    assert row.status == "running"
    assert row.image_digest == "sha256:" + "a" * 64
    assert row.health_check_timeout_seconds == 7
    assert reg.calls == 0
