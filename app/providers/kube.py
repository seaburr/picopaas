"""Real `KubeProvider` — NOT YET IMPLEMENTED.

TODO (docs/LEDGER.md "Now / Next"): the `kubernetes` client library hasn't
been added as a dependency yet (deliberately, per this chunk's scope). Once
it is, wire this up against app/k8s_manifests.py's rendered dicts (server-
side apply) and the reconciler loop. Use `app.providers.fake.FakeKubeProvider`
in tests until then.
"""
from __future__ import annotations

from typing import Any

from app.providers.base import KubeProvider


class KubeClientProvider(KubeProvider):
    def apply(self, manifests: list[dict[str, Any]]) -> None:
        raise NotImplementedError("TODO: kubernetes client dependency not added yet")

    def delete_namespace(self, namespace: str) -> None:
        raise NotImplementedError("TODO: kubernetes client dependency not added yet")

    def get_status(self, namespace: str) -> str:
        raise NotImplementedError("TODO: kubernetes client dependency not added yet")
