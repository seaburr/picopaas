"""`ObjectStore` backed by Garage (https://garagehq.deuxfleurs.fr/), the S3 store of the `generic` target.

Verified against Garage v2.5.0 (admin API **v2**, `Authorization: Bearer <admin_token>`, `/v2/<Action>`).

Mapping to the Spaces contract (see app/providers/digitalocean/spaces.py):
- Per-app bucket `pp-<app_id>-<6 random>` (global alias) and per-app key named `pp-<app_id>` (first) or
  `pp-<app_id>-<6 random>` (rotated). The store is stateless: both are found by name, as with Spaces.
- The app key is granted read+write on ONLY its bucket (`AllowBucketKey`). The platform key
  (OBJECT_STORE_ACCESS_KEY_ID/SECRET_ACCESS_KEY) is granted read+write on every app bucket at creation, and
  does the S3 work that needs no app key: usage listing, `delete_objects`, presign, emptying on delete.
- `set_read_only` is an IN-PLACE change of the key's grant (`DenyBucketKey` / `AllowBucketKey` on `write`).
  Unlike Spaces, whose grants are immutable, the access key does NOT change, so it returns None (the caller
  only persists/rolls the pod when creds are returned; the existing key just starts to behave read-only).
  Idempotent. If several keys exist (unfinished revoke) every key of the app gets the permission.
- `rotate_key` creates a NEW key with the same grant as the current one; the old one stays valid until
  `revoke_other_keys` (same persist-then-revoke protocol as Spaces, issue #2).
- `bucket_size_bytes` is the admin `bytes` counter (eventually consistent, normally within a second or two);
  `usage` lists objects (like Spaces) because it needs the per-prefix breakdown.
- Bucket quotas (maxSize) are not set: the platform enforces storage caps itself via the quota poll, as on DO.
- Addressing is path-style (Garage serves virtual-host style only when `root_domain` is configured), so the
  presigned URLs are `<endpoint>/<bucket>/<key>`. `endpoint` in the returned creds is OBJECT_STORE_ENDPOINT, the
  same URL the platform uses, and it is what apps see as `S3_ENDPOINT` / `${{ storage.endpoint }}`.
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

from app import config
from app.providers.base import ObjectStore, ObjectStoreError
from app.providers.digitalocean.spaces import summarize_objects

_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def _key_name(app_id: int) -> str:
    return f"pp-{app_id}"


class GarageStore(ObjectStore):
    def __init__(
        self,
        *,
        admin_url: str,
        admin_token: str,
        endpoint: str,
        region: str,
        access_key: str,
        secret_key: str,
        s3_client: Any = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self._admin = admin_url.rstrip("/")
        self._platform_key = access_key
        self._s3 = s3_client or boto3.client(
            "s3",
            region_name=region,
            endpoint_url=self.endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self._http = http_client or httpx.Client(timeout=20)
        self._headers = {"Authorization": f"Bearer {admin_token}"}

    @classmethod
    def from_env(cls) -> "GarageStore":
        """GARAGE_ADMIN_URL, GARAGE_ADMIN_TOKEN, OBJECT_STORE_ENDPOINT (S3 URL), OBJECT_STORE_REGION (default
        `garage`), OBJECT_STORE_ACCESS_KEY_ID / OBJECT_STORE_SECRET_ACCESS_KEY (the platform key)."""
        vals = {n: os.environ.get(n, "") for n in ("GARAGE_ADMIN_URL", "GARAGE_ADMIN_TOKEN", "OBJECT_STORE_ENDPOINT")}
        vals["OBJECT_STORE_ACCESS_KEY_ID"] = config.object_store_access_key_id()
        vals["OBJECT_STORE_SECRET_ACCESS_KEY"] = config.object_store_secret_access_key()
        for name, value in vals.items():
            if not value:
                raise ObjectStoreError(f"missing env var {name}")
        return cls(
            admin_url=vals["GARAGE_ADMIN_URL"], admin_token=vals["GARAGE_ADMIN_TOKEN"],
            endpoint=vals["OBJECT_STORE_ENDPOINT"], region=os.environ.get("OBJECT_STORE_REGION") or "garage",
            access_key=vals["OBJECT_STORE_ACCESS_KEY_ID"], secret_key=vals["OBJECT_STORE_SECRET_ACCESS_KEY"])

    # --- Garage admin API (v2) ----------------------------------------

    def _api(self, method: str, action: str, **kw: Any) -> Any:
        try:
            resp = self._http.request(method, f"{self._admin}/v2/{action}", headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise ObjectStoreError(f"Garage admin {action} failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise ObjectStoreError(f"Garage admin {action} -> {resp.status_code}")
        return resp.json() if resp.content else {}

    def _find_bucket_info(self, app_id: int) -> Optional[dict[str, Any]]:
        prefix = f"pp-{app_id}-"
        for b in self._api("GET", "ListBuckets"):
            for alias in b.get("globalAliases") or []:
                if alias.startswith(prefix):
                    return {"id": b["id"], "name": alias}
        return None

    def _find_bucket(self, app_id: int) -> Optional[str]:
        info = self._find_bucket_info(app_id)
        return info["name"] if info else None

    def _find_keys(self, app_id: int) -> list[dict[str, Any]]:
        """All keys of this app (more than one = a rotation whose old-key revoke didn't finish)."""
        base = _key_name(app_id)
        return [k for k in self._api("GET", "ListKeys")
                if k.get("name") == base or str(k.get("name", "")).startswith(base + "-")]

    def _grant(self, bucket_id: str, key_id: str, *, read: bool, write: bool) -> None:
        """Set exactly read/write for `key_id` on the bucket (never owner)."""
        perms = {"read": read, "write": write, "owner": False}
        allow = {k: True for k, v in perms.items() if v}
        deny = {k: True for k, v in perms.items() if not v}
        if allow:
            self._api("POST", "AllowBucketKey", json={"bucketId": bucket_id, "accessKeyId": key_id, "permissions": allow})
        if deny:
            self._api("POST", "DenyBucketKey", json={"bucketId": bucket_id, "accessKeyId": key_id, "permissions": deny})

    def _create_key(self, name: str) -> dict[str, Any]:
        key = self._api("POST", "CreateKey", json={"name": name})
        if not key.get("accessKeyId") or not key.get("secretAccessKey"):
            raise ObjectStoreError("Garage returned no key material")
        return key

    def _creds(self, bucket: str, key: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "region": self.region,
            "bucket": bucket,
            "access_key": key["accessKeyId"],
            "secret_key": key["secretAccessKey"],
        }

    def _app_permission(self, bucket_id: str, key_id: str) -> dict[str, bool]:
        info = self._api("GET", "GetKeyInfo", params={"id": key_id})
        for b in info.get("buckets") or []:
            if b.get("id") == bucket_id:
                return b.get("permissions") or {}
        return {}

    # --- S3 (objects) --------------------------------------------------

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

    def _iter_objects_safe(self, bucket: str):
        try:
            yield from self._iter_objects(bucket)
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"listing objects failed: {exc}") from exc

    # --- ObjectStore ---------------------------------------------------

    def create_bucket_and_key(self, app_id: int) -> dict[str, Any]:
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(6))
        bucket = f"pp-{app_id}-{suffix}"
        created = self._api("POST", "CreateBucket", json={"globalAlias": bucket})
        bucket_id = created["id"]
        key_id: Optional[str] = None
        try:
            key = self._create_key(_key_name(app_id))
            key_id = key["accessKeyId"]
            self._grant(bucket_id, key_id, read=True, write=True)
            self._grant(bucket_id, self._platform_key, read=True, write=True)
        except ObjectStoreError:
            for step in (lambda: key_id and self._api("POST", "DeleteKey", params={"id": key_id}),
                         lambda: self._api("POST", "DeleteBucket", params={"id": bucket_id})):
                try:  # don't leak an orphan bucket or key
                    step()
                except ObjectStoreError:
                    pass
            raise
        return self._creds(bucket, key)

    def delete(self, app_id: int) -> None:
        for key in self._find_keys(app_id):
            self._api("POST", "DeleteKey", params={"id": key["id"]})
        info = self._find_bucket_info(app_id)
        if info is None:
            return
        try:
            batch: list[dict[str, str]] = []
            for obj in self._iter_objects(info["name"]):
                batch.append({"Key": obj["Key"]})
                if len(batch) == 1000:
                    self._s3.delete_objects(Bucket=info["name"], Delete={"Objects": batch, "Quiet": True})
                    batch = []
            if batch:
                self._s3.delete_objects(Bucket=info["name"], Delete={"Objects": batch, "Quiet": True})
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"delete bucket failed: {exc}") from exc
        self._api("POST", "DeleteBucket", params={"id": info["id"]})

    def bucket_size_bytes(self, app_id: int) -> int:
        info = self._find_bucket_info(app_id)
        if info is None:
            return 0
        return int(self._api("GET", "GetBucketInfo", params={"id": info["id"]}).get("bytes") or 0)

    def usage(self, app_id: int) -> dict[str, Any]:
        bucket = self._find_bucket(app_id)
        if bucket is None:
            return {"size_bytes": 0, "objects": 0, "top_prefixes": []}
        return summarize_objects(self._iter_objects_safe(bucket))

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

    def rotate_key(self, app_id: int) -> dict[str, Any]:
        keys = self._find_keys(app_id)
        info = self._find_bucket_info(app_id)
        if not keys or info is None:
            raise ObjectStoreError(f"no key for app {app_id}")
        # Least privilege (PENTEST K65): the new key may write only if EVERY existing key can, and a permission the
        # admin API omits counts as no write, so a read-only app never gets a read-write key from a rotation.
        perms = [self._app_permission(info["id"], k["id"]) for k in keys]
        write = all(bool(p.get("write", False)) for p in perms)
        suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(6))
        new = self._create_key(f"{_key_name(app_id)}-{suffix}")
        try:
            self._grant(info["id"], new["accessKeyId"], read=True, write=write)
        except ObjectStoreError:
            try:
                self._api("POST", "DeleteKey", params={"id": new["accessKeyId"]})
            except ObjectStoreError:
                pass
            raise
        return self._creds(info["name"], new)

    def set_read_only(self, app_id: int, read_only: bool) -> Optional[dict[str, Any]]:
        """In place (see module docstring): the key does not change, so this always returns None."""
        keys = self._find_keys(app_id)
        info = self._find_bucket_info(app_id)
        if not keys or info is None:
            raise ObjectStoreError(f"no key for app {app_id}")
        for k in keys:
            self._grant(info["id"], k["id"], read=True, write=not read_only)
        return None

    def revoke_other_keys(self, app_id: int, keep_access_key: str) -> None:
        for key in self._find_keys(app_id):
            if key["id"] != keep_access_key:
                self._api("POST", "DeleteKey", params={"id": key["id"]})

    def delete_key(self, app_id: int, access_key: str) -> None:
        self._api("POST", "DeleteKey", params={"id": access_key})
