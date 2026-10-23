"""POST /apps/{id}/storage/presign: presigned PUT/GET for the app's own bucket (REQ-8)."""
import pytest

from app import config, models
from app.main import app as fastapi_app
from app.providers.fake import FakeMySQLAdmin, FakeObjectStore
from app.routers import usage
from app.routers.apps import get_mysql_admin_factory, get_object_store
from tests.conftest import register_and_login
from tests.test_usage_errors import APP, _provision

PUT = {"method": "PUT", "key": "bundle/site.tar.gz", "content_length": 1000}


class Ctx:
    def __init__(self):
        self.mysql, self.store = FakeMySQLAdmin(), FakeObjectStore()


@pytest.fixture()
def ctx():
    c = Ctx()
    fastapi_app.dependency_overrides[get_mysql_admin_factory] = lambda: (lambda s: c.mysql)
    fastapi_app.dependency_overrides[get_object_store] = lambda: c.store
    usage._presign_hits.clear()
    yield c
    fastapi_app.dependency_overrides.pop(get_mysql_admin_factory, None)
    fastapi_app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")  # first user is the platform admin
    return c


@pytest.fixture()
def owner(make_client, admin):
    c = make_client()
    register_and_login(c, "owner@example.com")
    return c


@pytest.fixture()
def aid(owner, db_session, ctx):
    i = owner.post("/apps", json=APP).json()["id"]
    _provision(db_session, i)
    return i


def _url(aid):
    return f"/apps/{aid}/storage/presign"


def test_owner_put_and_get(owner, aid, ctx):
    r = owner.post(_url(aid), json=PUT)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["method"] == "PUT" and b["headers"] == {"Content-Length": "1000"} and b["expires_at"]
    assert b["url"].startswith("https://") and "bundle/site.tar.gz" in b["url"]
    assert ctx.store.presigned[-1]["expires"] == 600
    g = owner.post(_url(aid), json={"method": "GET", "key": "a//b.txt", "expires_seconds": 60})
    assert g.status_code == 200 and g.json()["key"] == "a/b.txt" and g.json()["headers"] == {}


def test_admin_ok_other_user_404_no_storage_404(admin, owner, make_client, aid, ctx):
    assert admin.post(_url(aid), json=PUT).status_code == 200
    other = make_client()
    register_and_login(other, "other@example.com")
    assert other.post(_url(aid), json=PUT).status_code == 404
    bare = admin.post("/apps", json=dict(APP, name="bare")).json()["id"]
    assert admin.post(_url(bare), json=PUT).status_code == 404
    assert ctx.store.presigned.__len__() == 1


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "..", "a\\b", "a\x00b", "a\nb", "x" * 513, "a/.."])
def test_bad_keys_422(owner, aid, key):
    assert owner.post(_url(aid), json=dict(PUT, key=key)).status_code == 422


def test_expiry_bounds(owner, aid, ctx):
    for bad in (0, -1, 3601):
        assert owner.post(_url(aid), json=dict(PUT, expires_seconds=bad)).status_code == 422
    assert owner.post(_url(aid), json=dict(PUT, expires_seconds=3600)).status_code == 200
    assert ctx.store.presigned[-1]["expires"] == 3600


def test_put_needs_content_length_and_method(owner, aid):
    assert owner.post(_url(aid), json={"method": "PUT", "key": "k"}).status_code == 422
    assert owner.post(_url(aid), json={"method": "DELETE", "key": "k"}).status_code == 422


def test_size_over_remaining_quota_413(owner, aid, ctx):
    ctx.store.put_object(aid, "x", config.STORAGE_QUOTA_BYTES - 500)
    assert owner.post(_url(aid), json=dict(PUT, content_length=501)).status_code == 413
    assert owner.post(_url(aid), json=dict(PUT, content_length=500)).status_code == 200


def test_quota_override_is_honoured(owner, admin, aid, db_session):
    assert admin.patch(f"/admin/apps/{aid}/resources", json={"storage_quota_mb": 200}).status_code == 200
    assert owner.post(_url(aid), json=dict(PUT, content_length=200 * 1024 * 1024)).status_code == 200
    assert owner.post(_url(aid), json=dict(PUT, content_length=200 * 1024 * 1024 + 1)).status_code == 413


def test_over_quota_bucket_blocks_upload_not_download(owner, aid, db_session, ctx):
    db_session.query(models.AppStorage).filter_by(app_id=aid).one().over_quota = True
    db_session.commit()
    assert owner.post(_url(aid), json=PUT).status_code == 409
    assert owner.post(_url(aid), json={"method": "GET", "key": "k"}).status_code == 200


def test_read_scope_key(make_client, owner, aid):
    rk = owner.post("/api/keys", json={"scope": "read"}).json()["key"]
    fk = owner.post("/api/keys", json={"scope": "full"}).json()["key"]
    c = make_client()
    assert c.post(_url(aid), headers={"Authorization": f"Bearer {rk}"}, json=PUT).status_code == 403
    assert c.post(_url(aid), headers={"Authorization": f"Bearer {rk}"}, json={"method": "GET", "key": "k"}).status_code == 200
    assert c.post(_url(aid), headers={"Authorization": f"Bearer {fk}"}, json=PUT).status_code == 200


def test_audit_has_no_secrets(owner, aid, db_session, ctx):
    r = owner.post(_url(aid), json=PUT).json()
    row = db_session.query(models.AuditLog).filter_by(action="storage.presign").one()
    assert row.detail == "method=PUT key=bundle/site.tar.gz size=1000"
    assert "Signature" not in row.detail and r["url"] not in row.detail


def test_rate_limit(owner, aid):
    codes = [owner.post(_url(aid), json={"method": "GET", "key": "k"}).status_code for _ in range(usage.PRESIGN_PER_MINUTE + 1)]
    assert codes[-1] == 429 and codes[0] == 200


def test_real_store_signs_content_length():
    from app.providers.spaces import SpacesStore
    import boto3
    from botocore.config import Config
    s3 = boto3.client("s3", region_name="atl1", endpoint_url="https://atl1.digitaloceanspaces.com",
                      aws_access_key_id="AKIAFAKE", aws_secret_access_key="not-the-secret",
                      config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    st = SpacesStore(region="atl1", do_token="t", access_key="a", secret_key="b", s3_client=s3)
    st._find_bucket = lambda app_id: "pp-1-abc123"
    out = st.presign(1, "PUT", "k/x.bin", 600, 1234)
    assert "pp-1-abc123.atl1.digitaloceanspaces.com" in out["url"]
    assert "content-length" in out["url"].lower().split("x-amz-signedheaders=")[1].split("&")[0]
    assert "not-the-secret" not in out["url"]
