"""Real `KubeProvider` using the official `kubernetes` client.

Config: in-cluster service account first, falling back to the local kubeconfig.
`apply` is server-side apply (fieldManager "picopaas", conflicts forced) through
the dynamic client so any kind (incl. CRDs) works with no per-kind code.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.providers.base import KubeProvider, PodLogs

log = logging.getLogger(__name__)

FIELD_MANAGER = "picopaas"
# Container waiting reasons that mean the app will not come up without a change -> short codes the reconciler
# turns into human messages (app/reconciler.py STATUS_MESSAGES).
ERROR_CODES = {
    "ImagePullBackOff": "image_pull",
    "ErrImagePull": "image_pull",
    "CrashLoopBackOff": "crash_loop",
    "CreateContainerConfigError": "config",
}
APP_CONTAINER = "app"
MAX_LOG_LINE = 2000
PREVIOUS_TAIL = 50


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


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def pod_events(pod: dict[str, Any]) -> list[dict[str, Any]]:
    """What a pod tells us about restarts: an eviction, an OOM-killed or a crashed (non-zero exit) container.
    Keys are stable across sweeps (pod uid + restartCount/finishedAt), so a consumer can dedupe on them."""
    meta = pod.get("metadata") or {}
    st = pod.get("status") or {}
    uid = meta.get("uid") or meta.get("name") or "?"
    events: list[dict[str, Any]] = []
    if st.get("reason") == "Evicted":
        at = None
        for cond in st.get("conditions") or []:
            if cond.get("type") == "DisruptionTarget":
                at = _parse_ts(cond.get("lastTransitionTime"))
        at = at or _parse_ts(st.get("startTime")) or _parse_ts(meta.get("creationTimestamp")) or datetime.utcnow()
        kind = "disk" if "ephemeral" in (st.get("message") or "").lower() else "evicted"
        return [{"key": f"{uid}:evicted", "at": at, "kind": kind, "exit_code": None}]
    for cs in st.get("containerStatuses") or []:
        term = (cs.get("lastState") or {}).get("terminated") or {}
        if not term:
            continue
        code = term.get("exitCode")
        finished = term.get("finishedAt")
        at = _parse_ts(finished) or datetime.utcnow()
        key = f"{uid}:{cs.get('restartCount') or 0}:{finished or ''}"
        if term.get("reason") == "OOMKilled":
            events.append({"key": f"{key}:oom", "at": at, "kind": "oom", "exit_code": code})
        elif code not in (None, 0):
            events.append({"key": f"{key}:crashed", "at": at, "kind": "crashed", "exit_code": code})
    return events


def _as_dict(obj: Any) -> dict[str, Any]:
    return obj if isinstance(obj, dict) else obj.to_dict()


class KubeClientProvider(KubeProvider):
    def __init__(self, dynamic_client: Any | None = None, core_api: Any | None = None) -> None:
        self._dyn = dynamic_client
        self._core = core_api

    @property
    def core(self) -> Any:
        if self._core is None:
            from kubernetes import client

            _load_config()
            self._core = client.CoreV1Api()
        return self._core

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

    def delete_ingress(self, namespace: str, name: str) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        resource = self.dyn.resources.get(api_version="networking.k8s.io/v1", kind="Ingress")
        try:
            resource.delete(name=name, namespace=namespace)
        except NotFoundError:
            pass

    def list_app_namespaces(self) -> set[str]:
        resource = self.dyn.resources.get(api_version="v1", kind="Namespace")
        res = _as_dict(resource.get(label_selector="picopaas.io/app-id"))
        return {i["metadata"]["name"] for i in res.get("items", [])}

    def get_status(self, namespace: str) -> str:
        """'running' | 'deploying' | 'error: <code>' (codes: image_pull, crash_loop, oom, config, root_image,
        quota, replica_failure; the reconciler maps them to human text)."""
        pods = self.dyn.resources.get(api_version="v1", kind="Pod").get(namespace=namespace)
        items = _as_dict(pods).get("items", [])
        # Evicted pods linger as Failed; once a replacement is serving they are history, not an error.
        serving = any(
            (p.get("status") or {}).get("phase") == "Running"
            and any(c.get("ready") for c in (p.get("status") or {}).get("containerStatuses") or [])
            for p in items
        )
        for pod in items:
            code = self._pod_error(pod)
            if code == "disk" and serving:
                continue
            if code:
                return f"error: {code}"

        deployments = self.dyn.resources.get(api_version="apps/v1", kind="Deployment").get(namespace=namespace)
        items = _as_dict(deployments).get("items", [])
        if not items:
            return "deploying"
        dep = items[0]
        st = dep.get("status") or {}
        # The Deployment controller copies a ReplicaSet's FailedCreate (e.g. "exceeded quota") here. With an old
        # pod still ready this would otherwise look like a rollout that is merely slow.
        for cond in st.get("conditions") or []:
            if cond.get("type") == "ReplicaFailure" and str(cond.get("status")) == "True":
                return "error: quota" if "exceeded quota" in (cond.get("message") or "") else "error: replica_failure"
        ready = st.get("readyReplicas") or 0
        want = (dep.get("spec") or {}).get("replicas", 1)
        # A rollout in flight (or stuck) is "deploying", never "running":
        # the ready old pod would otherwise mask the new spec not being live.
        meta = dep.get("metadata") or {}
        if (st.get("observedGeneration") or 0) < meta.get("generation", 0):
            return "deploying"
        if (st.get("updatedReplicas") or 0) < want:
            return "deploying"
        return "running" if ready >= 1 else "deploying"

    def get_used_limits(self, namespace: str) -> dict[str, int] | None:
        """Sum of container limits of the pods that count against the ResourceQuota (non-terminal pods): exactly
        what the quota admission compares against. None when there are none."""
        pods = _as_dict(self.dyn.resources.get(api_version="v1", kind="Pod").get(namespace=namespace)).get("items", [])
        cpu = mem = eph = npods = 0
        found = False
        for pod in pods:
            if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
                continue
            npods += 1  # Terminating pods still count against the `pods` quota until they are gone
            found = True
            for c in (pod.get("spec") or {}).get("containers") or []:
                lim = (c.get("resources") or {}).get("limits") or {}
                if lim.get("cpu"):
                    cpu += parse_cpu_m(lim["cpu"])
                    found = True
                if lim.get("memory"):
                    mem += parse_mem_mi(lim["memory"])
                    found = True
                if lim.get("ephemeral-storage"):
                    eph += parse_mem_mi(lim["ephemeral-storage"])
                    found = True
        return {"cpu_limit_m": cpu, "mem_limit_mi": mem, "ephemeral_limit_mi": eph, "pods": npods} if found else None

    def app_pod_events(self) -> dict[str, list[dict[str, Any]]]:
        """One cluster-wide pod list per call; only app namespaces are looked at."""
        pods = _as_dict(self.dyn.resources.get(api_version="v1", kind="Pod").get()).get("items", [])
        out: dict[str, list[dict[str, Any]]] = {}
        for pod in pods:
            ns = (pod.get("metadata") or {}).get("namespace") or ""
            if ns.startswith("app-"):
                out.setdefault(ns, []).extend(pod_events(pod))
        return out

    def pending_hint(self, namespace: str) -> str:
        pods = _as_dict(self.dyn.resources.get(api_version="v1", kind="Pod").get(namespace=namespace)).get("items", [])
        hint = "none"
        for pod in pods:
            st = pod.get("status") or {}
            if st.get("phase") == "Running":
                return "not_ready"
            hint = "waiting"
        return hint

    @staticmethod
    def _created(pod: dict[str, Any]) -> str:
        meta = pod.get("metadata") or {}
        return str(meta.get("creationTimestamp") or meta.get("creation_timestamp") or "")

    def pod_logs(self, namespace: str, tail: int) -> PodLogs:
        # Only ever app namespaces (app-<id>); callers derive the name from the app id, never from user input.
        if not namespace.startswith("app-"):
            raise ValueError("not an app namespace")
        pods = _as_dict(self.dyn.resources.get(api_version="v1", kind="Pod").get(namespace=namespace)).get("items", [])
        pods = [p for p in pods if not (p.get("metadata") or {}).get("deletionTimestamp")] or pods
        if not pods:
            return PodLogs(pod=None, lines=[], note="No running pod yet.")
        pod = max(pods, key=self._created)
        name = pod["metadata"]["name"]
        cstat = ((pod.get("status") or {}).get("containerStatuses") or [{}])[0]
        restarts = int(cstat.get("restartCount") or 0)

        def read(previous: bool, n: int) -> list[str] | None:
            try:
                text = self.core.read_namespaced_pod_log(
                    name=name, namespace=namespace, container=APP_CONTAINER, tail_lines=n, previous=previous
                )
            except Exception as exc:  # noqa: BLE001 - container still creating / no previous container
                log.info("pod log read failed (%s, previous=%s): %s", name, previous, type(exc).__name__)
                return None
            return [ln[:MAX_LOG_LINE] for ln in (text or "").splitlines()]

        current = read(False, tail)
        lines: list[str] = []
        if restarts > 0:
            prev = read(True, min(tail, PREVIOUS_TAIL))
            if prev:
                lines += [f"--- previous container (before restart #{restarts}), last {len(prev)} lines ---", *prev,
                          "--- current container ---"]
        note = None
        if current is None:
            note = "The container hasn't started yet, so there are no logs."
        else:
            lines += current
            if not current and len(lines) == 0:
                note = "The container hasn't printed anything yet."
        return PodLogs(pod=name, lines=lines, note=note)

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
                "disk_capacity_mi": parse_mem_mi(cap["ephemeral-storage"]) if cap.get("ephemeral-storage") else None,
                "disk_allocatable_mi": parse_mem_mi(alloc["ephemeral-storage"]) if alloc.get("ephemeral-storage") else None,
                "conditions": {
                    t: any(c.get("type") == t and c.get("status") == "True" for c in st.get("conditions") or [])
                    for t in ("DiskPressure", "MemoryPressure", "PIDPressure")
                },
            })
        return {"nodes": nodes_out, "pods": pods_out, "metrics_available": metrics_ok, "errors": errors}

    @staticmethod
    def _pod_error(pod: dict[str, Any]) -> str | None:
        pst = pod.get("status") or {}
        if pst.get("reason") == "Evicted" and "ephemeral" in (pst.get("message") or "").lower():
            return "disk"
        statuses = pst.get("containerStatuses") or []
        for cs in statuses:
            state = cs.get("state") or {}
            waiting = state.get("waiting") or {}
            reason = waiting.get("reason")
            last = (cs.get("lastState") or {}).get("terminated") or {}
            if (state.get("terminated") or {}).get("reason") == "OOMKilled" or (
                reason == "CrashLoopBackOff" and last.get("reason") == "OOMKilled"
            ):
                return "oom"
            if reason in ERROR_CODES:
                msg = waiting.get("message") or ""
                if reason == "CreateContainerConfigError" and "runAsNonRoot" in msg:
                    return "root_image"
                return ERROR_CODES[reason]
        return None


# Alias used by app/scheduler.py.
KubeClient = KubeClientProvider
