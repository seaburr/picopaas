"""SpacesStore with botocore Stubber + httpx.MockTransport (no network)."""
from __future__ import annotations

import json

import boto3
import httpx
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from app.providers.base import ObjectStoreError
from app.providers.spaces import SpacesStore


def _s3():
    return boto3.client("s3", region_name="atl1", endpoint_url="https://atl1.digitaloceanspaces.com",
                        aws_access_key_id="a", aws_secret_access_key="b",
                        config=Config(s3={"addressing_style": "path"}))


class DO:
    """Minimal fake of /v2/spaces/keys."""

    def __init__(self, keys=None):
        self.keys = {k["access_key"]: k for k in (keys or [])}
        self.calls = []
        self.n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tok"
        path = req.url.path
        self.calls.append((req.method, path))
        if req.method == "GET":
            return httpx.Response(200, json={"keys": list(self.keys.values())})
        if req.method == "POST":
            body = json.loads(req.content)
            self.n += 1
            k = {"name": body["name"], "grants": body["grants"], "access_key": f"AK{self.n}", "secret_key": f"SK{self.n}"}
            self.keys[k["access_key"]] = k
            return httpx.Response(201, json={"key": k})
        ak = path.rsplit("/", 1)[1]
        if req.method == "DELETE":
            del self.keys[ak]
            return httpx.Response(204)
        if req.method == "PATCH":
            self.keys[ak]["name"] = json.loads(req.content)["name"]
            return httpx.Response(200, json={"key": self.keys[ak]})
        raise AssertionError(req)


def _store(do, stubber_client):
    return SpacesStore(region="atl1", do_token="tok", access_key="a", secret_key="b", s3_client=stubber_client,
                       http_client=httpx.Client(transport=httpx.MockTransport(do)))


def test_create_bucket_and_key():
    s3, do = _s3(), DO()
    with Stubber(s3) as st:
        st.add_response("create_bucket", {}, None)
        store = _store(do, s3)
        creds = store.create_bucket_and_key(7)
    assert creds["bucket"].startswith("pp-7-") and len(creds["bucket"]) == len("pp-7-") + 6
    assert creds["access_key"] == "AK1" and creds["region"] == "atl1"
    assert list(do.keys.values())[0]["grants"] == [{"bucket": creds["bucket"], "permission": "readwrite"}]


def test_bucket_size_paginates():
    s3 = _s3()
    with Stubber(s3) as st:
        st.add_response("list_buckets", {"Buckets": [{"Name": "pp-12-aaaaaa"}, {"Name": "pp-1-bbbbbb"}]})
        st.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "t",
                                            "Contents": [{"Key": "a", "Size": 100}]})
        st.add_response("list_objects_v2", {"IsTruncated": False, "Contents": [{"Key": "b", "Size": 50}]},
                        {"Bucket": "pp-1-bbbbbb", "ContinuationToken": "t"})
        st._queue[1]["expected_params"] = {"Bucket": "pp-1-bbbbbb"}  # first page has no token
        assert _store(DO(), s3).bucket_size_bytes(1) == 150


def test_delete_empties_bucket_then_removes_key():
    s3 = _s3()
    do = DO([{"name": "pp-1", "access_key": "AKx", "secret_key": "", "grants": [{"bucket": "pp-1-bbbbbb", "permission": "readwrite"}]}])
    with Stubber(s3) as st:
        st.add_response("list_buckets", {"Buckets": [{"Name": "pp-1-bbbbbb"}]})
        st.add_response("list_objects_v2", {"IsTruncated": False, "Contents": [{"Key": "a", "Size": 1}]})
        st.add_response("delete_objects", {}, {"Bucket": "pp-1-bbbbbb", "Delete": {"Objects": [{"Key": "a"}]}})
        st.add_response("delete_bucket", {}, {"Bucket": "pp-1-bbbbbb"})
        _store(do, s3).delete(1)
        st.assert_no_pending_responses()
    assert do.keys == {}


def _old(perm="readwrite"):
    return {"name": "pp-1", "access_key": "AKx", "secret_key": "", "grants": [{"bucket": "pp-1-bbbbbb", "permission": perm}]}


def test_set_read_only_creates_new_key_keeps_old_until_revoked():
    do = DO([_old()])
    store = _store(do, _s3())
    creds = store.set_read_only(1, True)
    assert creds["access_key"] == "AK1" and creds["bucket"] == "pp-1-bbbbbb"
    assert set(do.keys) == {"AKx", "AK1"}  # old key still valid until the caller persisted
    assert do.keys["AK1"]["grants"][0]["permission"] == "read"
    assert not any(m == "PATCH" for m, _ in do.calls)
    store.revoke_other_keys(1, "AK1")
    assert set(do.keys) == {"AK1"}
    assert store.set_read_only(1, True) is None
    assert store.set_read_only(1, False)["access_key"] == "AK2"


def test_rotate_key_keeps_grant_and_does_not_delete_old():
    do = DO([_old("read")])
    creds = _store(do, _s3()).rotate_key(1)
    assert set(do.keys) == {"AKx", creds["access_key"]}
    assert do.keys[creds["access_key"]]["grants"][0]["permission"] == "read"
    assert not any(m == "DELETE" for m, _ in do.calls)


def test_leftover_keys_trigger_swap_and_delete_removes_all():
    do = DO([_old(), dict(_old(), name="pp-1-abcdef", access_key="AKy"), dict(_old(), name="pp-12", access_key="AKz")])
    store = _store(do, _s3())
    assert store.set_read_only(1, False) is not None  # two keys -> not a no-op
    store.revoke_other_keys(1, "AK1")
    assert set(do.keys) == {"AK1", "AKz"}  # other app's key untouched


def test_delete_key_discards_new_key():
    do = DO([_old()])
    store = _store(do, _s3())
    new = store.rotate_key(1)
    store.delete_key(1, new["access_key"])
    assert set(do.keys) == {"AKx"}


def test_api_error_raises():
    store = _store(lambda r: httpx.Response(401), _s3())
    with pytest.raises(ObjectStoreError):
        store.rotate_key(1)
