"""GarageStore against an in-memory fake of the Garage admin API v2 (httpx.MockTransport) and a fake S3 client."""
import json

import httpx
import pytest
from botocore.exceptions import ClientError

from app.providers.base import ObjectStoreError
from app.providers.generic.garage import GarageStore

PLATFORM = "GKplatform"


class FakeGarageAdmin:
    def __init__(self):
        self.buckets = {}  # id -> alias
        self.keys = {}  # id -> name
        self.perms = {}  # (bucket_id, key_id) -> {read, write, owner}
        self.calls = []
        self.fail = set()
        self.n = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        action = req.url.path.rsplit("/", 1)[-1]
        self.calls.append(action)
        if action in self.fail:
            return httpx.Response(500)
        assert req.headers["authorization"] == "Bearer not-the-admin-token"
        body = json.loads(req.content) if req.content else {}
        q = dict(req.url.params)
        if action == "ListBuckets":
            return httpx.Response(200, json=[{"id": i, "globalAliases": [a]} for i, a in self.buckets.items()])
        if action == "CreateBucket":
            self.n += 1
            bid = f"b{self.n}"
            self.buckets[bid] = body["globalAlias"]
            return httpx.Response(200, json={"id": bid})
        if action == "DeleteBucket":
            self.buckets.pop(q["id"])
            return httpx.Response(204)
        if action == "ListKeys":
            return httpx.Response(200, json=[{"id": i, "name": n} for i, n in self.keys.items()])
        if action == "CreateKey":
            self.n += 1
            kid = f"GK{self.n}"
            self.keys[kid] = body["name"]
            if body["name"] == "pp-9-nomaterial":
                return httpx.Response(200, json={"accessKeyId": kid})
            return httpx.Response(200, json={"accessKeyId": kid, "secretAccessKey": f"not-secret-{self.n}"})
        if action == "DeleteKey":
            self.keys.pop(q["id"], None)
            return httpx.Response(204)
        if action in ("AllowBucketKey", "DenyBucketKey"):
            p = self.perms.setdefault((body["bucketId"], body["accessKeyId"]), {})
            for k in body["permissions"]:
                p[k] = action == "AllowBucketKey"
            return httpx.Response(200, json={})
        if action == "GetKeyInfo":
            return httpx.Response(200, json={"buckets": [
                {"id": b, "permissions": p} for (b, k), p in self.perms.items() if k == q["id"]]})
        if action == "GetBucketInfo":
            return httpx.Response(200, json={"bytes": 1234})
        raise AssertionError(action)


class FakeS3:
    def __init__(self, keys=(), page=1000, errors=None):
        self.keys = list(keys)
        self.page = page
        self.errors = errors
        self.deleted = []
        self.raise_list = False

    def list_objects_v2(self, **kw):
        if self.raise_list:
            raise ClientError({"Error": {"Code": "X", "Message": "boom"}}, "ListObjectsV2")
        start = int(kw.get("ContinuationToken") or 0)
        pre = kw.get("Prefix", "")
        ks = [k for k in self.keys if k.startswith(pre)]
        chunk = ks[start:start + self.page]
        out = {"Contents": [{"Key": k, "Size": 10} for k in chunk]}
        if start + self.page < len(ks):
            out.update(IsTruncated=True, NextContinuationToken=str(start + self.page))
        return out

    def delete_objects(self, Bucket, Delete):
        ks = [o["Key"] for o in Delete["Objects"]]
        self.deleted += ks
        return {"Errors": self.errors} if self.errors else {}

    def generate_presigned_url(self, op, Params, ExpiresIn, HttpMethod):
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?op={op}"


@pytest.fixture()
def env():
    admin = FakeGarageAdmin()
    s3 = FakeS3()
    store = GarageStore(
        admin_url="http://garage:3903/", admin_token="not-the-admin-token", endpoint="http://garage:3900/",
        region="garage", access_key=PLATFORM, secret_key="not-the-secret", s3_client=s3,
        http_client=httpx.Client(transport=httpx.MockTransport(admin.handler)))
    return store, admin, s3


def test_create_bucket_and_key_grants_app_and_platform(env):
    store, admin, _ = env
    c = store.create_bucket_and_key(7)
    assert c["bucket"].startswith("pp-7-") and c["endpoint"] == "http://garage:3900"
    bid = next(iter(admin.buckets))
    assert admin.perms[(bid, c["access_key"])] == {"read": True, "write": True, "owner": False}
    assert admin.perms[(bid, PLATFORM)]["write"] is True


def test_create_failure_cleans_up_bucket_and_key(env):
    store, admin, _ = env
    admin.fail.add("AllowBucketKey")
    with pytest.raises(ObjectStoreError):
        store.create_bucket_and_key(7)
    assert admin.buckets == {} and admin.keys == {}


def test_create_key_without_material_is_rejected(env):
    store, admin, _ = env
    admin.fail.add("DeleteKey")  # cleanup errors are swallowed
    store._create_key  # noqa: B018
    with pytest.raises(ObjectStoreError, match="no key material"):
        store._create_key("pp-9-nomaterial")


def test_admin_http_error_and_transport_error(env):
    store, admin, _ = env
    admin.fail.add("ListBuckets")
    with pytest.raises(ObjectStoreError, match="500"):
        store.bucket_size_bytes(1)

    def boom(req):
        raise httpx.ConnectError("down")
    store._http = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(ObjectStoreError, match="ConnectError"):
        store.usage(1)


def test_size_and_usage_and_missing_bucket(env):
    store, admin, s3 = env
    assert store.bucket_size_bytes(7) == 0
    assert store.usage(7)["objects"] == 0
    assert store.delete_objects(7) == {"deleted": 0, "truncated": False}
    store.create_bucket_and_key(7)
    assert store.bucket_size_bytes(7) == 1234
    s3.keys = ["a/1", "a/2", "b/1"]
    s3.page = 2
    u = store.usage(7)
    assert u["objects"] == 3 and u["size_bytes"] == 30
    s3.raise_list = True
    with pytest.raises(ObjectStoreError, match="listing objects failed"):
        store.usage(7)


def test_delete_removes_keys_objects_bucket(env):
    store, admin, s3 = env
    store.delete(7)  # nothing there: no-op
    store.create_bucket_and_key(7)
    s3.keys = [f"k{i}" for i in range(1500)]
    store.delete(7)
    assert admin.buckets == {} and admin.keys == {} and len(s3.deleted) == 1500
    store.create_bucket_and_key(8)
    s3.raise_list = True
    with pytest.raises(ObjectStoreError, match="delete bucket failed"):
        store.delete(8)


def test_delete_objects_prefix_limit_and_errors(env):
    store, admin, s3 = env
    store.create_bucket_and_key(7)
    s3.keys = ["a/1", "a/2", "a/3", "b/1"]
    assert store.delete_objects(7, prefix="a/") == {"deleted": 3, "truncated": False}
    s3.keys = ["x1", "x2", "x3"]
    s3.page = 2
    assert store.delete_objects(7, max_objects=2) == {"deleted": 2, "truncated": True}
    s3.errors = [{"Key": "x1"}]
    with pytest.raises(ObjectStoreError, match="could not be deleted"):
        store.delete_objects(7)
    s3.errors = None
    s3.raise_list = True
    with pytest.raises(ObjectStoreError, match="delete objects failed"):
        store.delete_objects(7)


def test_presign(env):
    store, _, _ = env
    with pytest.raises(ObjectStoreError, match="bucket not found"):
        store.presign(7, "GET", "k", 60)
    store.create_bucket_and_key(7)
    assert "get_object" in store.presign(7, "GET", "k", 60)["url"]
    put = store.presign(7, "PUT", "k", 60, content_length=5)
    assert put["headers"] == {"Content-Length": "5"} and "put_object" in put["url"]
    with pytest.raises(ObjectStoreError, match="content_length"):
        store.presign(7, "PUT", "k", 60)


def test_read_only_toggle_and_rotation_least_privilege(env):
    store, admin, _ = env
    first = store.create_bucket_and_key(7)
    bid = next(iter(admin.buckets))
    assert store.set_read_only(7, True) is None
    assert admin.perms[(bid, first["access_key"])]["write"] is False
    rotated = store.rotate_key(7)  # read-only app must not get a read-write key
    assert admin.perms[(bid, rotated["access_key"])] == {"read": True, "write": False, "owner": False}
    assert rotated["access_key"] != first["access_key"]
    store.revoke_other_keys(7, rotated["access_key"])
    assert list(admin.keys) == [rotated["access_key"]]
    store.set_read_only(7, False)
    assert admin.perms[(bid, rotated["access_key"])]["write"] is True
    store.delete_key(7, rotated["access_key"])
    assert admin.keys == {}
    with pytest.raises(ObjectStoreError, match="no key"):
        store.rotate_key(7)
    with pytest.raises(ObjectStoreError, match="no key"):
        store.set_read_only(7, True)


def test_rotate_grant_failure_deletes_new_key(env):
    store, admin, _ = env
    store.create_bucket_and_key(7)
    before = set(admin.keys)
    admin.fail.update({"AllowBucketKey", "DeleteKey"})
    with pytest.raises(ObjectStoreError):
        store.rotate_key(7)
    assert before <= set(admin.keys)


def test_from_env(monkeypatch):
    for n in ("GARAGE_ADMIN_URL", "GARAGE_ADMIN_TOKEN", "OBJECT_STORE_ENDPOINT",
              "OBJECT_STORE_ACCESS_KEY_ID", "OBJECT_STORE_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(ObjectStoreError, match="missing env var"):
        GarageStore.from_env()
    monkeypatch.setenv("GARAGE_ADMIN_URL", "http://g:3903")
    monkeypatch.setenv("GARAGE_ADMIN_TOKEN", "not-the-admin-token")
    monkeypatch.setenv("OBJECT_STORE_ENDPOINT", "http://g:3900")
    monkeypatch.setenv("OBJECT_STORE_ACCESS_KEY_ID", "GKplatform")
    monkeypatch.setenv("OBJECT_STORE_SECRET_ACCESS_KEY", "not-the-secret")
    s = GarageStore.from_env()
    assert s.region == "garage" and s.endpoint == "http://g:3900"
