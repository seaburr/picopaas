"""One ObjectStore contract, run against the fake and (when GARAGE_TEST_* is set) a real Garage.

Real Garage: `scripts/dev/garage-up.sh && source .garage-dev/garage.env`, then run this file. Without those env
vars the real variants are skipped, so the normal suite stays offline.

The suite talks to the store only through the interface plus a small `Driver` that plays the part of an app holding
the creds (put/get with an S3 client for Garage; a state-based simulation of key and grant rules for the fake).
"""
import os
import random
import time

import boto3
import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.providers.base import ObjectStoreError
from app.providers.fake import FakeObjectStore
from app.providers.generic.garage import GarageStore

REAL_ENV = ("GARAGE_TEST_ADMIN_URL", "GARAGE_TEST_ADMIN_TOKEN", "GARAGE_TEST_S3_ENDPOINT",
            "GARAGE_TEST_ACCESS_KEY_ID", "GARAGE_TEST_SECRET_ACCESS_KEY")
HAVE_GARAGE = all(os.environ.get(n) for n in REAL_ENV)


class Denied(Exception):
    """The store refused the request made with an app key."""


class GarageDriver:
    """An app: boto3 with the app's own creds, path-style, against the test S3 endpoint."""

    def __init__(self) -> None:
        self.endpoint = os.environ["GARAGE_TEST_S3_ENDPOINT"]
        self.region = os.environ.get("GARAGE_TEST_REGION", "garage")

    def _client(self, creds):
        return boto3.client("s3", region_name=self.region, endpoint_url=self.endpoint,
                            aws_access_key_id=creds["access_key"], aws_secret_access_key=creds["secret_key"],
                            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                          retries={"max_attempts": 1}))

    def put(self, creds, bucket, key, data: bytes) -> None:
        try:
            self._client(creds).put_object(Bucket=bucket, Key=key, Body=data)
        except (ClientError, BotoCoreError) as exc:
            raise Denied(str(exc)) from exc

    def get(self, creds, bucket, key) -> bytes:
        try:
            return self._client(creds).get_object(Bucket=bucket, Key=key)["Body"].read()
        except (ClientError, BotoCoreError) as exc:
            raise Denied(str(exc)) from exc

    def presigned(self, method, presign, data: bytes = b"") -> bytes:
        resp = httpx.request(method, presign["url"], headers=presign["headers"], content=data if method == "PUT" else None)
        assert resp.status_code == 200, resp.status_code
        return resp.content

    def settle(self, store, app_id, want_bytes) -> None:
        """Garage's size counter is eventually consistent."""
        for _ in range(40):
            if store.bucket_size_bytes(app_id) == want_bytes:
                return
            time.sleep(0.25)


class FakeDriver:
    """State-based stand-in for the S3 side of FakeObjectStore (valid keys, grants, read-only)."""

    def __init__(self, store: FakeObjectStore) -> None:
        self.store = store
        self.data: dict[tuple[int, str], bytes] = {}

    def _owner(self, creds, bucket) -> int:
        for app_id, c in self.store.creds.items():
            if c["bucket"] == bucket:
                if creds["access_key"] not in self.store.valid_keys.get(app_id, set()) or creds["bucket"] != bucket:
                    raise Denied("key not valid for this bucket")
                return app_id
        raise Denied("no such bucket")

    def put(self, creds, bucket, key, data: bytes) -> None:
        app_id = self._owner(creds, bucket)
        if self.store.read_only.get(app_id):
            raise Denied("read-only")
        self.data[(app_id, key)] = data
        self.store.put_object(app_id, key, len(data))

    def get(self, creds, bucket, key) -> bytes:
        app_id = self._owner(creds, bucket)
        try:
            return self.data[(app_id, key)]
        except KeyError:
            raise Denied("no such key") from None

    def presigned(self, method, presign, data: bytes = b"") -> bytes:
        assert presign["url"].startswith("https://")
        key = presign["url"].split("?")[0].split("/", 3)[-1]
        app_id = next(a for a, c in self.store.creds.items() if c["bucket"] in presign["url"])
        if method == "PUT":
            self.data[(app_id, key)] = data
            self.store.put_object(app_id, key, len(data))
            return b""
        return self.data[(app_id, key)]

    def settle(self, store, app_id, want_bytes) -> None:
        pass


@pytest.fixture(params=["fake", pytest.param("garage", marks=pytest.mark.skipif(
    not HAVE_GARAGE, reason="set GARAGE_TEST_* (scripts/dev/garage-up.sh) to run against a real Garage"))])
def backend(request):
    created: list[int] = []
    if request.param == "fake":
        store = FakeObjectStore()
        driver = FakeDriver(store)
        next_id = iter(range(1, 1000))
    else:
        store = GarageStore(
            admin_url=os.environ["GARAGE_TEST_ADMIN_URL"], admin_token=os.environ["GARAGE_TEST_ADMIN_TOKEN"],
            endpoint=os.environ["GARAGE_TEST_S3_ENDPOINT"], region=os.environ.get("GARAGE_TEST_REGION", "garage"),
            access_key=os.environ["GARAGE_TEST_ACCESS_KEY_ID"], secret_key=os.environ["GARAGE_TEST_SECRET_ACCESS_KEY"])
        driver = GarageDriver()
        base = random.randint(10**6, 10**9)
        next_id = iter(range(base, base + 1000))

    def new_app():
        app_id = next(next_id)
        created.append(app_id)
        return app_id, store.create_bucket_and_key(app_id)

    yield store, driver, new_app
    for app_id in created:
        try:
            store.delete(app_id)
        except ObjectStoreError:
            pass


def _flip(store, app_id, creds, read_only):
    """The caller protocol: persist the new creds if the key changed, then revoke the others."""
    new = store.set_read_only(app_id, read_only)
    if new:
        store.revoke_other_keys(app_id, new["access_key"])
        return new
    return creds


def test_create_returns_creds(backend):
    store, _, new_app = backend
    app_id, creds = new_app()
    assert set(creds) >= {"endpoint", "region", "bucket", "access_key", "secret_key"}
    assert creds["bucket"].startswith(f"pp-{app_id}")
    assert creds["access_key"] and creds["secret_key"]


def test_app_key_can_put_and_get(backend):
    store, driver, new_app = backend
    _, creds = new_app()
    driver.put(creds, creds["bucket"], "hello.txt", b"hi there")
    assert driver.get(creds, creds["bucket"], "hello.txt") == b"hi there"


def test_app_key_cannot_touch_another_apps_bucket(backend):
    store, driver, new_app = backend
    _, a = new_app()
    _, b = new_app()
    driver.put(b, b["bucket"], "secret.txt", b"b-data")
    with pytest.raises(Denied):
        driver.put(a, b["bucket"], "x.txt", b"nope")
    with pytest.raises(Denied):
        driver.get(a, b["bucket"], "secret.txt")


def test_read_only_blocks_writes_but_allows_reads(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    driver.put(creds, creds["bucket"], "keep.txt", b"data")
    creds = _flip(store, app_id, creds, True)
    with pytest.raises(Denied):
        driver.put(creds, creds["bucket"], "new.txt", b"data")
    assert driver.get(creds, creds["bucket"], "keep.txt") == b"data"


def test_set_read_only_is_idempotent(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    creds = _flip(store, app_id, creds, True)
    creds = _flip(store, app_id, creds, True)
    with pytest.raises(Denied):
        driver.put(creds, creds["bucket"], "new.txt", b"data")


def test_restore_readwrite_after_read_only(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    creds = _flip(store, app_id, creds, True)
    creds = _flip(store, app_id, creds, False)
    driver.put(creds, creds["bucket"], "again.txt", b"back")
    assert driver.get(creds, creds["bucket"], "again.txt") == b"back"


def test_rotate_old_key_works_until_revoked_then_dies(backend):
    store, driver, new_app = backend
    app_id, old = new_app()
    driver.put(old, old["bucket"], "a.txt", b"1")
    new = store.rotate_key(app_id)
    assert new["access_key"] != old["access_key"] and new["bucket"] == old["bucket"]
    assert driver.get(old, old["bucket"], "a.txt") == b"1"  # persist first, revoke after (issue #2)
    store.revoke_other_keys(app_id, new["access_key"])
    with pytest.raises(Denied):
        driver.get(old, old["bucket"], "a.txt")
    assert driver.get(new, new["bucket"], "a.txt") == b"1"
    driver.put(new, new["bucket"], "b.txt", b"2")


def test_rotate_keeps_read_only(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    creds = _flip(store, app_id, creds, True)
    new = store.rotate_key(app_id)
    store.revoke_other_keys(app_id, new["access_key"])
    with pytest.raises(Denied):
        driver.put(new, new["bucket"], "x.txt", b"no")


def test_delete_key_discards_an_unpersisted_new_key(backend):
    store, driver, new_app = backend
    app_id, old = new_app()
    new = store.rotate_key(app_id)
    store.delete_key(app_id, new["access_key"])
    with pytest.raises(Denied):
        driver.put(new, new["bucket"], "x.txt", b"no")
    driver.put(old, old["bucket"], "x.txt", b"yes")


def test_presign_get_and_put(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    put = store.presign(app_id, "PUT", "up/file.bin", 300, content_length=5)
    assert put["url"].startswith("http") and put["headers"] == {"Content-Length": "5"}
    driver.presigned("PUT", put, b"12345")
    get = store.presign(app_id, "GET", "up/file.bin", 300)
    assert get["headers"] == {}
    assert driver.presigned("GET", get) == b"12345"
    assert driver.get(creds, creds["bucket"], "up/file.bin") == b"12345"


def test_presign_put_requires_content_length(backend):
    store, _, new_app = backend
    app_id, _ = new_app()
    with pytest.raises(ObjectStoreError):
        store.presign(app_id, "PUT", "x", 60)


def test_usage_and_size(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    assert store.usage(app_id)["objects"] == 0
    driver.put(creds, creds["bucket"], "img/a.png", b"a" * 100)
    driver.put(creds, creds["bucket"], "img/b.png", b"b" * 50)
    driver.put(creds, creds["bucket"], "doc/c.txt", b"c" * 10)
    driver.put(creds, creds["bucket"], "root.txt", b"r" * 5)
    driver.settle(store, app_id, 165)
    usage = store.usage(app_id)
    assert usage["size_bytes"] == 165 and usage["objects"] == 4
    top = {t["prefix"]: t for t in usage["top_prefixes"]}
    assert top["img/"] == {"prefix": "img/", "size_bytes": 150, "objects": 2}
    assert top[""]["size_bytes"] == 5
    assert usage["top_prefixes"][0]["prefix"] == "img/"
    assert store.bucket_size_bytes(app_id) == 165


def test_delete_objects_by_prefix_then_all_with_cap(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    for name in ("p/1", "p/2", "p/3", "q/1"):
        driver.put(creds, creds["bucket"], name, b"x")
    assert store.delete_objects(app_id, prefix="q/") == {"deleted": 1, "truncated": False}
    assert store.delete_objects(app_id, max_objects=2) == {"deleted": 2, "truncated": True}
    assert store.delete_objects(app_id) == {"deleted": 1, "truncated": False}
    assert store.usage(app_id)["objects"] == 0


def test_delete_removes_bucket_and_keys(backend):
    store, driver, new_app = backend
    app_id, creds = new_app()
    driver.put(creds, creds["bucket"], "a.txt", b"1")
    store.delete(app_id)
    with pytest.raises(Denied):
        driver.get(creds, creds["bucket"], "a.txt")
    assert store.usage(app_id) == {"size_bytes": 0, "objects": 0, "top_prefixes": []}
    assert store.bucket_size_bytes(app_id) == 0
    store.delete(app_id)  # idempotent
