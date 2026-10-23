"""Real `ObjectStore` backed by DigitalOcean Spaces.

Bucket operations go through the S3-compatible endpoint with boto3 (using a
platform-level Spaces key). Per-app keys go through the DO API
(`/v2/spaces/keys`, DIGITALOCEAN_TOKEN): POST accepts `grants:
[{bucket, permission}]` with permission `read` / `readwrite`, DELETE removes a
key. Verified against the DO API reference: PUT/PATCH on a key can ONLY rename
it, grants are immutable. So "toggle the grant" (`set_read_only`, rotation) is
implemented as create-new-key-with-new-grant, which means the app's access key
CHANGES when its read/write state flips; `set_read_only` / `rotate_key` return
the new creds (or None if nothing changed). They do NOT delete the old key:
the caller must persist the new creds (commit) and only then call
`revoke_other_keys(app_id, new_access_key)`; if persisting fails it calls
`delete_key` on the new key instead. So the stored key always works (issue #2).
Keys are named `pp-<app_id>` (first) or `pp-<app_id>-<6 random>` (swapped);
no rename step (it was cosmetic and a failure point).

The store is stateless: an app's key is the one named `pp-<app_id>`, and its
bucket is the one named `pp-<app_id>-<6 random>` (names are global in Spaces,
hence the suffix).
"""
from __future__ import annotations

import os
import secrets
import string
from typing import Any, Optional

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.providers.base import ObjectStore, ObjectStoreError

DO_API = "https://api.digitalocean.com"
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def _key_name(app_id: int) -> str:
    return f"pp-{app_id}"


def summarize_objects(objects, *, max_scan: int = 50000, top_n: int = 5) -> dict[str, Any]:
    """Totals plus the biggest first-path-segment prefixes, from an iterable of {"Key", "Size"}."""
    size = count = 0
    groups: dict[str, list[int]] = {}
    for o in objects:
        if count >= max_scan:
            break
        n = int(o["Size"])
        size += n
        count += 1
        key = str(o["Key"])
        prefix = key.split("/", 1)[0] + "/" if "/" in key else ""
        g = groups.setdefault(prefix, [0, 0])
        g[0] += n
        g[1] += 1
    top = sorted(groups.items(), key=lambda kv: kv[1][0], reverse=True)[:top_n]
    return {
        "size_bytes": size,
        "objects": count,
        "top_prefixes": [{"prefix": k, "size_bytes": v[0], "objects": v[1]} for k, v in top],
    }


class SpacesStore(ObjectStore):
    def __init__(
        self,
        *,
        region: str,
        do_token: str,
        access_key: str,
        secret_key: str,
        s3_client: Any = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.region = region
        self.endpoint = f"https://{region}.digitaloceanspaces.com"
        self._s3 = s3_client or boto3.client(
            "s3",
            region_name=region,
            endpoint_url=self.endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        self._http = http_client or httpx.Client(timeout=20)
        self._headers = {"Authorization": f"Bearer {do_token}"}

    @classmethod
    def from_env(cls) -> "SpacesStore":
        """DIGITALOCEAN_TOKEN, SPACES_ACCESS_KEY_ID, SPACES_SECRET_ACCESS_KEY (platform-wide
        full-access key for bucket ops), SPACES_REGION (default atl1)."""
        try:
            return cls(
                region=os.environ.get("SPACES_REGION", "atl1"),
                do_token=os.environ["DIGITALOCEAN_TOKEN"],
                access_key=os.environ["SPACES_ACCESS_KEY_ID"],
                secret_key=os.environ["SPACES_SECRET_ACCESS_KEY"],
            )
        except KeyError as exc:
            raise ObjectStoreError(f"missing env var {exc.args[0]}") from exc

    # --- DO API (keys) -------------------------------------------------

    def _api(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            resp = self._http.request(method, DO_API + path, headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise ObjectStoreError(f"DO API {method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ObjectStoreError(f"DO API {method} {path} -> {resp.status_code}")
        return resp.json() if resp.content else {}

    def _list_keys(self) -> list[dict[str, Any]]:
        keys: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._api("GET", "/v2/spaces/keys", params={"page": page, "per_page": 200})
            batch = data.get("keys") or []
            keys.extend(batch)
            if len(batch) < 200:
                return keys
            page += 1

    def _find_keys(self, app_id: int) -> list[dict[str, Any]]:
        """All keys of this app (more than one = a swap whose old-key revoke didn't finish)."""
        base = _key_name(app_id)
        return [
            k for k in self._list_keys()
            if k.get("name") == base or str(k.get("name", "")).startswith(base + "-")
        ]

    def _create_key(self, name: str, bucket: str, permission: str) -> dict[str, Any]:
        data = self._api(
            "POST",
            "/v2/spaces/keys",
            json={"name": name, "grants": [{"bucket": bucket, "permission": permission}]},
        )
        key = data.get("key") or {}
        if not key.get("access_key") or not key.get("secret_key"):
            raise ObjectStoreError("DO API returned no key material")
        return key

    def _creds(self, bucket: str, key: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "region": self.region,
            "bucket": bucket,
            "access_key": key["access_key"],
            "secret_key": key["secret_key"],
        }

    # --- S3 (buckets) --------------------------------------------------

    def _find_bucket(self, app_id: int) -> Optional[str]:
        try:
            buckets = self._s3.list_buckets().get("Buckets", [])
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"list_buckets failed: {exc}") from exc
        prefix = f"pp-{app_id}-"
        for b in buckets:
            if b["Name"].startswith(prefix):
                return b["Name"]
        return None

    def _iter_objects(self, bucket: str):
        token = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            yield from resp.get("Contents", [])
            if not resp.get("IsTruncated"):
                return
            token = resp["NextContinuationToken"]

    # --- ObjectStore ---------------------------------------------------

    def create_bucket_and_key(self, app_id: int) -> dict[str, Any]:
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(6))
        bucket = f"pp-{app_id}-{suffix}"
        try:
            self._s3.create_bucket(Bucket=bucket, ACL="private")
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"create_bucket failed: {exc}") from exc
        try:
            key = self._create_key(_key_name(app_id), bucket, "readwrite")
        except ObjectStoreError:
            try:  # don't leak an orphan bucket
                self._s3.delete_bucket(Bucket=bucket)
            except (ClientError, BotoCoreError):
                pass
            raise
        return self._creds(bucket, key)

    def delete(self, app_id: int) -> None:
        for key in self._find_keys(app_id):
            self._api("DELETE", f"/v2/spaces/keys/{key['access_key']}")
        bucket = self._find_bucket(app_id)
        if bucket is None:
            return
        try:
            batch: list[dict[str, str]] = []
            for obj in self._iter_objects(bucket):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    self._s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                    batch = []
            if batch:
                self._s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
            self._s3.delete_bucket(Bucket=bucket)
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"delete bucket failed: {exc}") from exc

    def bucket_size_bytes(self, app_id: int) -> int:
        bucket = self._find_bucket(app_id)
        if bucket is None:
            return 0
        try:
            return sum(int(o["Size"]) for o in self._iter_objects(bucket))
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"bucket_size_bytes failed: {exc}") from exc

    def usage(self, app_id: int) -> dict[str, Any]:
        bucket = self._find_bucket(app_id)
        if bucket is None:
            return {"size_bytes": 0, "objects": 0, "top_prefixes": []}
        return summarize_objects(self._iter_objects_safe(bucket))

    def _iter_objects_safe(self, bucket: str):
        try:
            yield from self._iter_objects(bucket)
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"listing objects failed: {exc}") from exc

    def delete_objects(self, app_id: int, prefix: Optional[str] = None, max_objects: int = 5000) -> dict[str, Any]:
        bucket = self._find_bucket(app_id)
        if bucket is None:
            return {"deleted": 0, "truncated": False}
        deleted = 0
        truncated = False
        try:
            token = None
            while True:
                kwargs: dict[str, Any] = {"Bucket": bucket, "MaxKeys": 1000}
                if prefix:
                    kwargs["Prefix"] = prefix
                if token:
                    kwargs["ContinuationToken"] = token
                resp = self._s3.list_objects_v2(**kwargs)
                keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
                room = max_objects - deleted
                if len(keys) > room:
                    keys, truncated = keys[:room], True
                if keys:
                    out = self._s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
                    errs = out.get("Errors") or []
                    deleted += len(keys) - len(errs)
                    if errs:
                        raise ObjectStoreError(f"{len(errs)} object(s) could not be deleted")
                if truncated:
                    break
                if not resp.get("IsTruncated"):
                    break
                if deleted >= max_objects:
                    truncated = True
                    break
                token = resp["NextContinuationToken"]
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"delete objects failed: {exc}") from exc
        return {"deleted": deleted, "truncated": truncated}

    def presign(self, app_id: int, method: str, key: str, expires_seconds: int,
                content_length: Optional[int] = None) -> dict[str, Any]:
        bucket = self._find_bucket(app_id)
        if bucket is None:
            raise ObjectStoreError("bucket not found")
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        headers: dict[str, str] = {}
        if method == "PUT":
            if content_length is None:
                raise ObjectStoreError("content_length is required for PUT")
            params["ContentLength"] = content_length
            headers["Content-Length"] = str(content_length)
        try:
            url = self._s3.generate_presigned_url(
                "put_object" if method == "PUT" else "get_object", Params=params,
                ExpiresIn=expires_seconds, HttpMethod=method)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError(str(exc)) from exc
        return {"url": url, "headers": headers}

    def _swap_key(self, app_id: int, old: dict[str, Any], permission: str) -> dict[str, Any]:
        grants = old.get("grants") or []
        if not grants:
            raise ObjectStoreError("existing key has no bucket grant")
        bucket = grants[0]["bucket"]
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(6))
        new = self._create_key(f"{_key_name(app_id)}-{suffix}", bucket, permission)
        return self._creds(bucket, new)

    def rotate_key(self, app_id: int) -> dict[str, Any]:
        keys = self._find_keys(app_id)
        if not keys:
            raise ObjectStoreError(f"no key for app {app_id}")
        permission = (keys[0].get("grants") or [{}])[0].get("permission", "readwrite")
        return self._swap_key(app_id, keys[0], permission)

    def set_read_only(self, app_id: int, read_only: bool) -> Optional[dict[str, Any]]:
        keys = self._find_keys(app_id)
        if not keys:
            raise ObjectStoreError(f"no key for app {app_id}")
        want = "read" if read_only else "readwrite"
        # Several keys = unfinished revoke; swap anyway so the caller's revoke cleans up.
        if len(keys) == 1 and (keys[0].get("grants") or [{}])[0].get("permission") == want:
            return None
        return self._swap_key(app_id, keys[0], want)

    def revoke_other_keys(self, app_id: int, keep_access_key: str) -> None:
        for key in self._find_keys(app_id):
            if key["access_key"] != keep_access_key:
                self._api("DELETE", f"/v2/spaces/keys/{key['access_key']}")

    def delete_key(self, app_id: int, access_key: str) -> None:
        self._api("DELETE", f"/v2/spaces/keys/{access_key}")
