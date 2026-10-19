"""Real `KubeProvider` using the official `kubernetes` client.

Config: in-cluster service account first, falling back to the local kubeconfig.
`apply` is server-side apply (fieldManager "picopaas", conflicts forced) through
the dynamic client so any kind (incl. CRDs) works with no per-kind code.
"""
from __future__ import annotations

import logging
from typing import Any

from app.providers.base import KubeProvider

log = logging.getLogger(__name__)

FIELD_MANAGER = "picopaas"
# Container waiting reasons that mean the app will not come up without a change.
ERROR_REASONS = ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError")


def _load_config() -> None:
    from kubernetes import config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _default_dynamic_client() -> Any:
    from kubernetes import client, dynamic

    _load_config()
    return dynamic.DynamicClient(client.ApiClient())


def _as_dict(obj: Any) -> dict[str, Any]:
    return obj if isinstance(obj, dict) else obj.to_dict()


class KubeClientProvider(KubeProvider):
    def __init__(self, dynamic_client: Any | None = None) -> None:
        self._dyn = dynamic_client

    @property
    def dyn(self) -> Any:
        if self._dyn is None:
            self._dyn = _default_dynamic_client()
        return self._dyn

    def apply(self, manifests: list[dict[str, Any]]) -> None:
        # Namespaces first so namespaced objects can land (callers also flatten in apply order).
        ordered = sorted(manifests, key=lambda m: m.get("kind") != "Namespace")
        for m in ordered:
            resource = self.dyn.resources.get(api_version=m["apiVersion"], kind=m["kind"])
            meta = m["metadata"]
            self.dyn.server_side_apply(
                resource,
                body=m,
                name=meta["name"],
                namespace=meta.get("namespace"),
                force_conflicts=True,
                field_manager=FIELD_MANAGER,
            )

    def delete_namespace(self, namespace: str) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        resource = self.dyn.resources.get(api_version="v1", kind="Namespace")
        try:
            resource.delete(name=namespace)
        except NotFoundError:
            pass

    def get_status(self, namespace: str) -> str:
        """'running' | 'deploying' | 'error: <reason>' (the reconciler stores it as-is)."""
        pods = self.dyn.resources.get(api_version="v1", kind="Pod").get(namespace=namespace)
        for pod in _as_dict(pods).get("items", []):
            reason = self._pod_error(pod)
            if reason:
                return f"error: {reason}"

        deployments = self.dyn.resources.get(api_version="apps/v1", kind="Deployment").get(namespace=namespace)
        items = _as_dict(deployments).get("items", [])
        if not items:
            return "deploying"
        ready = (items[0].get("status") or {}).get("readyReplicas") or 0
        return "running" if ready >= 1 else "deploying"

    @staticmethod
    def _pod_error(pod: dict[str, Any]) -> str | None:
        statuses = (pod.get("status") or {}).get("containerStatuses") or []
        for cs in statuses:
            waiting = ((cs.get("state") or {}).get("waiting")) or {}
            reason = waiting.get("reason")
            if reason in ERROR_REASONS:
                msg = waiting.get("message") or ""
                if reason == "CreateContainerConfigError" and "runAsNonRoot" in msg:
                    return "image runs as root (runAsNonRoot)"
                return reason
        return None


# Alias used by app/scheduler.py.
KubeClient = KubeClientProvider
