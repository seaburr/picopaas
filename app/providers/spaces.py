"""Real `ObjectStore` — NOT YET IMPLEMENTED.

TODO (docs/LEDGER.md "Now / Next"): needs the DO Spaces (S3-compatible) API
for bucket + per-bucket access key lifecycle (ARCHITECTURE.md §2 "Storage").
⚠ Also blocked on the open spike noted in ARCHITECTURE.md: confirm the DO API
(`/v2/spaces/keys`) actually supports per-bucket keys now; if not, fall back
to a shared bucket + presigned-URL proxy. Use `app.providers.fake.FakeObjectStore`
in tests until then.
"""
from __future__ import annotations

from typing import Any

from app.providers.base import ObjectStore


class SpacesObjectStore(ObjectStore):
    def create_bucket_and_key(self, app_id: int) -> dict[str, Any]:
        raise NotImplementedError("TODO: DO Spaces client not wired yet")

    def delete(self, app_id: int) -> None:
        raise NotImplementedError("TODO: DO Spaces client not wired yet")

    def bucket_size_bytes(self, app_id: int) -> int:
        raise NotImplementedError("TODO: DO Spaces client not wired yet")

    def set_read_only(self, app_id: int, read_only: bool) -> None:
        raise NotImplementedError("TODO: DO Spaces client not wired yet")
