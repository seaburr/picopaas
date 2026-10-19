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


def parse_cpu_m(q: Any) -> int:
    """Kubernetes CPU quantity ("250m", "1", "1000000n") -> millicores (int)."""
    from kubernetes.utils import parse_quantity

    return int(round(parse_quantity(str(q)) * 1000))


def parse_mem_mi(q: Any) -> int:
    """Kubernetes memory quantity ("512Mi", "1Gi", "123456Ki") -> whole MiB (int, floored)."""
    from kubernetes.utils import parse_quantity

    return int(parse_quantity(str(q)) // (1024 * 1024))


def _sum_requests(pod: dict[str, Any]) -> tuple[int, int]:
    cpu = mem = 0
    for c in (pod.get("spec") or {}).get("containers") or []:
        res = c.get("resources") or {}
        req = res.get("requests") or res.get("limits") or {}  # k8s defaults requests to limits
        if req.get("cpu"):
            cpu += parse_cpu_m(req["cpu"])
        if req.get("memory"):
            mem += parse_mem_mi(req["memory"])
    return cpu, mem


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

    def cluster_snapshot(self) -> dict[str, Any]:
        """Nodes + non-terminated pods (+ metrics.k8s.io usage when available). Read-only, resources only."""
        import datetime as dt

        from kubernetes import client

        _load_config()
        core = client.CoreV1Api()
        errors: list[str] = []
        node_items = [_as_dict(n) for n in core.list_node().items]
        pod_items = [
            p for p in (_as_dict(x) for x in core.list_pod_for_all_namespaces().items)
            if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")
        ]

        node_use: dict[str, dict[str, Any]] = {}
        pod_use: dict[tuple[str, str], dict[str, Any]] = {}
        metrics_ok = True
        try:
            co = client.CustomObjectsApi()
            for item in co.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes").get("items", []):
                node_use[item["metadata"]["name"]] = item.get("usage") or {}
            for item in co.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods").get("items", []):
                cpu = mem = 0
                for c in item.get("containers") or []:
                    u = c.get("usage") or {}
                    cpu += parse_cpu_m(u.get("cpu") or "0")
                    mem += parse_mem_mi(u.get("memory") or "0")
                pod_use[(item["metadata"]["namespace"], item["metadata"]["name"])] = {"cpu": cpu, "mem": mem}
        except Exception as exc:  # noqa: BLE001 - metrics API missing/forbidden must not break the dashboard
            metrics_ok = False
            node_use, pod_use = {}, {}
            errors.append(f"metrics.k8s.io unavailable ({type(exc).__name__})")

        now = dt.datetime.now(dt.timezone.utc)
        reqs: dict[str, list[int]] = {}
        counts: dict[str, int] = {}
        pods_out = []
        for p in pod_items:
            meta, st = p.get("metadata") or {}, p.get("status") or {}
            node = (p.get("spec") or {}).get("node_name") or (p.get("spec") or {}).get("nodeName") or ""
            cpu_r, mem_r = _sum_requests(p)
            r = reqs.setdefault(node, [0, 0])
            r[0] += cpu_r
            r[1] += mem_r
            counts[node] = counts.get(node, 0) + 1
            cstat = st.get("container_statuses") or st.get("containerStatuses") or []
            use = pod_use.get((meta.get("namespace"), meta.get("name")))
            pods_out.append({
                "name": meta.get("name"), "namespace": meta.get("namespace"), "node": node,
                "phase": st.get("phase"),
                "ready": bool(cstat) and all(c.get("ready") for c in cstat),
                "restarts": sum(int(c.get("restart_count", c.get("restartCount", 0)) or 0) for c in cstat),
                "cpu_used_m": use["cpu"] if use else None, "mem_used_mi": use["mem"] if use else None,
            })

        nodes_out = []
        for n in node_items:
            meta, st = n.get("metadata") or {}, n.get("status") or {}
            name = meta.get("name")
            cap, alloc = st.get("capacity") or {}, st.get("allocatable") or {}
            created = meta.get("creation_timestamp") or meta.get("creationTimestamp")
            if isinstance(created, str):
                created = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            use = node_use.get(name)
            nodes_out.append({
                "name": name,
                "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or []),
                "age_s": int((now - created).total_seconds()) if created else 0,
                "cpu_capacity_m": parse_cpu_m(cap.get("cpu", "0")),
                "cpu_allocatable_m": parse_cpu_m(alloc.get("cpu", "0")),
                "cpu_requested_m": reqs.get(name, [0, 0])[0],
                "cpu_used_m": parse_cpu_m(use["cpu"]) if use and use.get("cpu") else None,
                "mem_capacity_mi": parse_mem_mi(cap.get("memory", "0")),
                "mem_allocatable_mi": parse_mem_mi(alloc.get("memory", "0")),
                "mem_requested_mi": reqs.get(name, [0, 0])[1],
                "mem_used_mi": parse_mem_mi(use["memory"]) if use and use.get("memory") else None,
                "pods": counts.get(name, 0),
                "pods_capacity": int(alloc.get("pods") or cap.get("pods") or 0),
            })
        return {"nodes": nodes_out, "pods": pods_out, "metrics_available": metrics_ok, "errors": errors}

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
