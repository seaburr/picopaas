"""Pure functions that render k8s manifests (as plain dicts) for one app.

No cluster access happens here — these functions only build data. Applying
them is `app/providers/base.py::KubeProvider.apply()`'s job (real impl TODO,
see `app/providers/kube.py`). Mirrors docs/ARCHITECTURE.md §2 "Tenancy and
isolation" and "Reference variables".

Root images are NOT supported (Chas, 2026-10-18). RUN_AS_NON_ROOT stays True, which
matches the `restricted` Pod Security level enforced on every app namespace. Images
must declare a non-root USER. See docs/ARCHITECTURE.md "Tenancy and isolation".
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import (
    APP_DOMAIN,
    CUSTOM_DOMAIN_ISSUER,
    DEFAULT_CPU_LIMIT_M,
    DEFAULT_CPU_REQUEST_M,
    DEFAULT_MEM_LIMIT_MI,
    DEFAULT_MEM_REQUEST_MI,
    EPHEMERAL_LIMIT_MI,
    EGRESS_BANDWIDTH_MBIT,
    EPHEMERAL_REQUEST_MI,
    INGRESS_CLASS,
    INGRESS_NAMESPACE,
    VOLUME_FS_GROUP,
    VOLUME_STORAGE_CLASS,
)

RUN_AS_NON_ROOT = True

# ResourceQuota / LimitRange values (ARCHITECTURE.md §2 "Compute").
# The quota admits (replicas + 1) pods' worth of the per-pod caps (app/config.py), which the LimitRange / container
# limits keep. Replicas are set by the platform admin (App.replicas, default 1) and tenants can't create pods, so the
# extra pod is only ever used by the surge pod during a rolling update (maxSurge 1); without it the surge is rejected with
# "exceeded quota: app-quota" and the rollout never completes.
POD_LIMIT_CPU = f"{DEFAULT_CPU_LIMIT_M}m"  # per-pod caps (what the health page shows)
POD_LIMIT_MEMORY = f"{DEFAULT_MEM_LIMIT_MI}Mi"
POD_LIMIT_EPHEMERAL = f"{EPHEMERAL_LIMIT_MI}Mi"
QUOTA_SERVICES = "1"

LIMIT_RANGE_DEFAULT_CPU = POD_LIMIT_CPU
LIMIT_RANGE_DEFAULT_MEMORY = POD_LIMIT_MEMORY
LIMIT_RANGE_DEFAULT_REQUEST_CPU = f"{DEFAULT_CPU_REQUEST_M}m"
LIMIT_RANGE_DEFAULT_REQUEST_MEMORY = f"{DEFAULT_MEM_REQUEST_MI}Mi"
LIMIT_RANGE_DEFAULT_REQUEST_EPHEMERAL = f"{EPHEMERAL_REQUEST_MI}Mi"

DNS_NAMESPACE = "kube-system"
DNS_PORT = 53

# RFC1918 + link-local ranges excluded from the "internet" egress allowance,
# so apps can reach the public internet but not other private cluster/VPC IPs.
PRIVATE_CIDRS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",  # link-local, incl. cloud metadata 169.254.169.254
]

# Non-routable / special-use ranges that never hold a legitimate public destination. Excluded from the internet
# egress allowance too (defence in depth: DOKS pod/service CIDRs 10.244/16 + 10.245/16 are already inside 10/8).
# Kept separate from PRIVATE_CIDRS because the reconciler uses that list to detect VPC-private storage IPs.
RESERVED_CIDRS = [
    "0.0.0.0/8",       # "this network"
    "100.64.0.0/10",   # CGNAT / shared address space (some clouds/overlays route internal services here)
    "127.0.0.0/8",     # loopback
    "192.0.0.0/24",    # IETF protocol assignments
    "198.18.0.0/15",   # benchmarking
    "224.0.0.0/4",     # multicast
    "240.0.0.0/4",     # reserved (incl. 255.255.255.255 broadcast)
]
EGRESS_EXCEPT_CIDRS = PRIVATE_CIDRS + RESERVED_CIDRS

MYSQL_PORT = 3306


BACKUP_EGRESS_POLICY = "allow-egress-backup"  # only rendered while volume backups are on for an app with a volume
SAME_NAMESPACE_POLICY = "allow-same-namespace"  # only rendered when replicas > 1


def namespace_name(app_id: int) -> str:
    return f"app-{app_id}"


def _resource_name(app_id: int) -> str:
    return f"app-{app_id}"


def _secret_name(app_id: int) -> str:
    return f"app-{app_id}-env"


def pvc_name(app_id: int) -> str:
    return f"app-{app_id}-data"


def _full_hostname(hostname: str) -> str:
    return f"{hostname}.{APP_DOMAIN}"


def _to_cidr(host_or_cidr: str) -> str:
    """Normalize a bare host/IP to a /32 CIDR; pass through anything already a CIDR."""
    return host_or_cidr if "/" in host_or_cidr else f"{host_or_cidr}/32"


def namespace_manifest(app_id: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace_name(app_id),
            "labels": {
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "picopaas.io/app-id": str(app_id),
            },
        },
    }


# Namespaced grant for the platform ServiceAccount (deploy/platform/rbac.yaml defines the ClusterRole
# `picopaas-app-ns`: secrets/resourcequotas/limitranges/services). Bound per app namespace with a RoleBinding so the
# grant is only valid inside app-<id>; the SA needs `bind` on exactly that ClusterRole (no `escalate`, so it can
# create this binding even once it no longer holds cluster-wide Secret access).
PLATFORM_SA_NAME = "picopaas"
PLATFORM_SA_NAMESPACE = "picopaas-system"
APP_NS_CLUSTERROLE = "picopaas-app-ns"


def role_binding_manifest(app_id: int) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "picopaas-platform", "namespace": namespace_name(app_id)},
        "subjects": [{"kind": "ServiceAccount", "name": PLATFORM_SA_NAME, "namespace": PLATFORM_SA_NAMESPACE}],
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": APP_NS_CLUSTERROLE},
    }


def _res(resources: dict[str, int] | None) -> dict[str, int]:
    from app import resources as r

    return resources or r.effective(None)


def _eph(e: dict[str, int]) -> int:
    """Per-pod scratch-disk limit (MiB): the app's effective value, else the platform default."""
    return e.get("ephemeral_limit_mi") or EPHEMERAL_LIMIT_MI


BACKUP_POD_LABEL = "picopaas.io/backup"  # on every backup Job pod (app/providers/backup.py LABEL); apps' own pods never have it


def backup_pod_resources(resources: dict[str, int] | None = None) -> dict[str, int]:
    """Size of the one volume-backup pod that runs in an app namespace: the platform's backup pod (200m / 320Mi / 200Mi scratch)
    clamped to the app's own per-container limits, because the namespace LimitRange rejects a container above them."""
    e = _res(resources)
    eph = min(200, _eph(e))
    return {
        "cpu_limit_m": min(200, e["cpu_limit_m"]), "mem_limit_mi": min(320, e["mem_limit_mi"]), "eph_limit_mi": eph,
        "cpu_request_m": min(50, e["cpu_limit_m"]), "mem_request_mi": min(128, e["mem_limit_mi"]), "eph_request_mi": min(64, eph),
    }


def resource_quota_manifest(
    app_id: int, resources: dict[str, int] | None = None, used_limits: dict[str, int] | None = None,
    custom_domain: bool = False, replicas: int = 1, volume: dict[str, Any] | None = None,
    backup_pod: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Quota = max((replicas + 1) x new limits, limits already used by existing pods + new limits): the surge pod of a rollout
    runs the NEW limits while the old pod still consumes its own (possibly bigger) limits. `used_limits`
    ({"cpu_limit_m", "mem_limit_mi", "ephemeral_limit_mi", "pods"}, summed over the pods in the cluster) is None when there are no pods.

    `custom_domain` gives one extra Service and one extra Pod of headroom: cert-manager's HTTP-01 solver creates a
    temporary (ClusterIP) Service + Pod in this namespace while issuing/renewing the custom-domain cert, which would
    otherwise be rejected by the services=1 / pods=2 caps. NodePort/LoadBalancer services stay forbidden.

    `backup_pod` (backup_pod_resources(), only for an app with a volume while volume backups are on) adds exactly one backup
    pod's worth of pods / CPU / memory / scratch on top, so a backup Job fits next to the app and its rollout surge pod."""
    e = _res(resources)
    used = used_limits or {}
    slots = max(1, int(replicas)) + 1  # steady replicas + one rolling-update surge pod
    cpu_total = max(slots * e["cpu_limit_m"], (used.get("cpu_limit_m") or 0) + e["cpu_limit_m"])
    mem_total = max(slots * e["mem_limit_mi"], (used.get("mem_limit_mi") or 0) + e["mem_limit_mi"])
    eph = _eph(e)
    eph_total = max(slots * eph, (used.get("ephemeral_limit_mi") or 0) + eph)
    # Pod count too: a Terminating old pod still counts, so back-to-back rollouts (e.g. storage key swap then
    # recovery roll) need (existing + 1) slots, not a fixed 2. +1 more when a custom domain may run an ACME solver pod.
    solver_headroom = 1 if custom_domain else 0
    pods_total = max(slots + solver_headroom, (used.get("pods") or 0) + 1 + solver_headroom)
    services_total = int(QUOTA_SERVICES) + solver_headroom
    if backup_pod:
        cpu_total += backup_pod["cpu_limit_m"]
        mem_total += backup_pod["mem_limit_mi"]
        eph_total += backup_pod["eph_limit_mi"]
        pods_total += 1
    # With a volume: exactly one PVC and exactly its size, so nothing in the namespace can claim more storage.
    volume_quota = (
        {"persistentvolumeclaims": "1", "requests.storage": f"{int(volume['size_gi'])}Gi"} if volume else {}
    )
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "app-quota", "namespace": namespace_name(app_id)},
        "spec": {
            "hard": {
                **volume_quota,
                "limits.cpu": f"{cpu_total}m",
                "limits.memory": f"{mem_total}Mi",
                "requests.cpu": f"{cpu_total}m",
                "requests.memory": f"{mem_total}Mi",
                "limits.ephemeral-storage": f"{eph_total}Mi",
                "requests.ephemeral-storage": f"{eph_total}Mi",
                "pods": str(pods_total),
                "services": str(services_total),
                # No LoadBalancer/NodePort services — the ingress controller (Traefik) is the only entry point, and
                # the ACME solver uses a ClusterIP service (ARCHITECTURE.md's no-LB lean option).
                "services.loadbalancers": "0",
                "services.nodeports": "0",
            }
        },
    }


def limit_range_manifest(app_id: int, resources: dict[str, int] | None = None) -> dict[str, Any]:
    e = _res(resources)
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "app-limits", "namespace": namespace_name(app_id)},
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "max": {"cpu": f"{e['cpu_limit_m']}m", "memory": f"{e['mem_limit_mi']}Mi",
                            "ephemeral-storage": f"{_eph(e)}Mi"},
                    "default": {"cpu": f"{e['cpu_limit_m']}m", "memory": f"{e['mem_limit_mi']}Mi",
                                "ephemeral-storage": f"{_eph(e)}Mi"},
                    "defaultRequest": {"cpu": f"{e['cpu_request_m']}m", "memory": f"{e['mem_request_mi']}Mi",
                                       "ephemeral-storage": LIMIT_RANGE_DEFAULT_REQUEST_EPHEMERAL},
                }
            ]
        },
    }


SMTP_PORTS = (25, 465, 587)
# Outbound abuse ports commonly blocked by hosting providers (inclusive (lo, hi) ranges). SSH (22), NTP (123),
# DNS (53) and database ports are deliberately NOT blocked: git-over-ssh, clock sync, custom resolvers and
# external managed databases are legitimate. See docs/PENTEST.md "Abuse hardening review (2026-10-22)".
ABUSE_TCP_RANGES = (
    (19, 19),      # chargen
    (23, 23),      # telnet
    (135, 139),    # MS-RPC / NetBIOS
    (445, 445),    # SMB
    (3389, 3389),  # RDP
    (6660, 6669),  # IRC (botnet C2)
    (6697, 6697),  # IRC over TLS
)
ABUSE_UDP_RANGES = (
    (19, 19),        # chargen
    (111, 111),      # rpcbind (amplification)
    (135, 139),      # RPC / NetBIOS
    (161, 161),      # SNMP (amplification)
    (389, 389),      # CLDAP (amplification)
    (445, 445),      # SMB
    (1900, 1900),    # SSDP (amplification)
    (3389, 3389),    # RDP
    (11211, 11211),  # memcached (amplification)
)


def _allowed_ranges(protocol: str, blocked: list[tuple[int, int]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    start = 1
    for lo, hi in sorted(blocked):
        if lo > start:
            out.append({"protocol": protocol, "port": start, "endPort": lo - 1})
        start = max(start, hi + 1)
    if start <= 65535:
        out.append({"protocol": protocol, "port": start, "endPort": 65535})
    return out


def _internet_egress_ports() -> list[dict[str, Any]]:
    tcp_blocked = [(p, p) for p in SMTP_PORTS] + list(ABUSE_TCP_RANGES)
    return _allowed_ranges("TCP", tcp_blocked) + _allowed_ranges("UDP", list(ABUSE_UDP_RANGES))


INGRESS_EGRESS_POLICY = "allow-egress-ingress"
INGRESS_POD_LABELS = {"app.kubernetes.io/name": "traefik"}
INGRESS_WEB_PORTS = (8000, 8443, 80, 443)

# cert-manager's ACME HTTP-01 solver pod listens on this port (its --listen-port default) and is labelled
# acme.cert-manager.io/http01-solver=true. Traefik must reach it there to serve the challenge.
ACME_SOLVER_PORT = 8089
ACME_SOLVER_LABEL = {"acme.cert-manager.io/http01-solver": "true"}


def network_policies(
    app_id: int,
    app_port: int,
    mysql_cidr: str | None,
    mysql_port: int | None = None,
    storage_ips: list[str] | None = None,
    custom_domain: bool = False,
    replicas: int = 1,
    backup_ips: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Default-deny plus narrow allow rules. `backup_ips` (not None) means volume backups run in this namespace: the app's
    allow-* rules then skip the backup Job pod (label picopaas.io/backup) and a dedicated `allow-egress-backup` policy gives
    that pod DNS plus the backup bucket on 443 and nothing else (`backup_ips` = the bucket endpoint's private VPC IPs).

    Kubernetes NetworkPolicy semantics are additive per matched pod: the
    "default-deny" policy below isolates every pod in the namespace for both
    Ingress and Egress (empty rule lists), and each "allow-*" policy adds one
    specific allowance on top. The union across all four is what actually
    takes effect.
    """
    namespace = namespace_name(app_id)
    all_pods_selector: dict[str, Any] = {"podSelector": {}}
    # NetworkPolicies are additive per pod, so a Job pod would otherwise inherit the app's internet / database / storage rules.
    app_pods_selector: dict[str, Any] = (
        {"podSelector": {}} if backup_ips is None
        else {"podSelector": {"matchExpressions": [{"key": BACKUP_POD_LABEL, "operator": "DoesNotExist"}]}}
    )

    default_deny = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": namespace},
        "spec": {
            **all_pods_selector,
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    }

    allow_ingress_controller = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-ingress-controller", "namespace": namespace},
        "spec": {
            **app_pods_selector,
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": INGRESS_NAMESPACE
                                }
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": app_port}],
                }
            ],
        },
    }

    allow_egress_dns = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-egress-dns", "namespace": namespace},
        "spec": {
            **all_pods_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": DNS_NAMESPACE}
                            }
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": DNS_PORT},
                        {"protocol": "TCP", "port": DNS_PORT},
                    ],
                }
            ],
        },
    }

    allow_egress_internet = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-egress-internet", "namespace": namespace},
        "spec": {
            **app_pods_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": list(EGRESS_EXCEPT_CIDRS),
                            }
                        }
                    ],
                    # NetworkPolicy can't deny, so allow everything EXCEPT SMTP and the abuse ports
                    # (see _internet_egress_ports) via endPort ranges (K8s >= 1.25).
                    "ports": _internet_egress_ports(),
                }
            ],
        },
    }

    # One DB-egress policy serves both engines: `mysql_cidr`/`mysql_port` are the app's DB server's CIDR/port
    # (reconciler picks the pg CIDR + 25060 for a Postgres-backed app), so an app only gets egress for its own engine.
    # mysql_cidr=None -> in-cluster MySQL (ns picopaas-data, pod app=mysql);
    # otherwise a managed DB reached by CIDR.
    if mysql_cidr is None:
        mysql_peer: dict[str, Any] = {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "picopaas-data"}},
            "podSelector": {"matchLabels": {"app": "mysql"}},
        }
    else:
        mysql_peer = {"ipBlock": {"cidr": _to_cidr(mysql_cidr)}}

    allow_egress_mysql = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-egress-mysql", "namespace": namespace},
        "spec": {
            **app_pods_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [mysql_peer],
                    "ports": [{"protocol": "TCP", "port": mysql_port or MYSQL_PORT}],
                }
            ],
        },
    }

    # Calls to any https://*.picopaas.xyz (the platform API, other apps, the app's own URL) target the load balancer's
    # public IP, which kube-proxy short-circuits to the Traefik pods' private IPs; the internet rule excludes 10/8, so
    # without this rule those calls fail. Scoped to the Traefik pods on their web entrypoint ports only (after DNAT the
    # destination is the container port: 8000/8443 in the chart, 80/443 kept for a hostPort or port change).
    allow_egress_ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": INGRESS_EGRESS_POLICY, "namespace": namespace},
        "spec": {
            **app_pods_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": INGRESS_NAMESPACE}},
                            "podSelector": {"matchLabels": dict(INGRESS_POD_LABELS)},
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": p} for p in INGRESS_WEB_PORTS],
                }
            ],
        },
    }

    policies = [
        default_deny,
        allow_ingress_controller,
        allow_egress_dns,
        allow_egress_internet,
        allow_egress_mysql,
        allow_egress_ingress,
    ]
    if custom_domain:
        # Let Traefik reach the ACME HTTP-01 solver pod (port 8089). Scoped to the solver pods by label, so the
        # app's own pods still only accept traffic on app_port. Transient — the solver exists only during
        # issuance/renewal of the custom-domain cert.
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-acme-solver", "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": dict(ACME_SOLVER_LABEL)},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": INGRESS_NAMESPACE}}}
                        ],
                        "ports": [{"protocol": "TCP", "port": ACME_SOLVER_PORT}],
                    }
                ],
            },
        })
    if replicas > 1:
        # HA/clustered apps (e.g. gossip peers) need pod-to-pod traffic. Strictly namespace-scoped: a bare
        # podSelector (no namespaceSelector) only matches pods in THIS policy's namespace, so no cross-tenant path.
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": SAME_NAMESPACE_POLICY, "namespace": namespace},
            "spec": {
                **app_pods_selector,
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [{"from": [{"podSelector": {}}]}],
                "egress": [{"to": [{"podSelector": {}}]}],
            },
        })
    if storage_ips:
        # Spaces resolves to a private VPC IP inside DO; the internet rule excludes PRIVATE_CIDRS,
        # so allow exactly those /32s on 443 (never the whole VPC range).
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-egress-storage", "namespace": namespace},
            "spec": {
                **app_pods_selector,
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"ipBlock": {"cidr": f"{ip}/32"}} for ip in sorted(set(storage_ips))],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
            },
        })
    if backup_ips is not None:
        backup_to: list[dict[str, Any]] = [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(EGRESS_EXCEPT_CIDRS)}}]
        backup_to += [{"ipBlock": {"cidr": f"{ip}/32"}} for ip in sorted(set(backup_ips))]
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": BACKUP_EGRESS_POLICY, "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": {BACKUP_POD_LABEL: "1"}},
                "policyTypes": ["Egress"],
                # The backup bucket only: HTTPS to public addresses (Spaces has no fixed range) plus the endpoint's private VPC /32s.
                # DNS comes from allow-egress-dns, which still selects every pod. No database, no other namespace, no metadata.
                "egress": [{"to": backup_to, "ports": [{"protocol": "TCP", "port": 443}]}],
            },
        })
    return policies


def _env_checksum(env: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]


def secret_manifest(app_id: int, env: dict[str, str]) -> dict[str, Any]:
    """`env` must already be resolved (see app/env_refs.py::resolve) — no `${{ }}` refs here."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": _secret_name(app_id), "namespace": namespace_name(app_id)},
        "type": "Opaque",
        "stringData": dict(env),
    }


def deployment_manifest(
    app_id: int,
    image: str,
    port: int,
    env_checksum: str = "",
    replicas: int = 1,
    health_check_enabled: bool = False,
    health_check_path: str = "/healthz",
    user_env: dict[str, str] | None = None,
    resources: dict[str, int] | None = None,
    run_as_uid: int | None = None,
    restart_nonce: str | None = None,
    image_ref: str | None = None,
    health_check_timeout_seconds: int | None = None,
    volume: dict[str, Any] | None = None,
) -> dict[str, Any]:
    e = _res(resources)
    name = _resource_name(app_id)
    if volume:
        replicas = min(replicas, 1)  # ReadWriteOnce: one pod at a time (0 stays 0 when suspended)
    pod_labels = {"app": name}
    # Changing env (e.g. rotated creds) changes this annotation, which rolls the pod;
    # envFrom alone never restarts a running pod.
    pod_annotations = {"picopaas.io/env-checksum": env_checksum} if env_checksum else {}
    # Outbound bandwidth cap enforced by the Cilium bandwidth manager (includes responses to visitors). Changing it rolls pods.
    pod_annotations["kubernetes.io/egress-bandwidth"] = f"{e.get('egress_mbit') or EGRESS_BANDWIDTH_MBIT}M"
    if restart_nonce:  # POST /apps/{id}/restart: a new nonce changes the pod template, so the pod rolls
        pod_annotations["picopaas.io/restart-nonce"] = restart_nonce
    if health_check_enabled:
        # A user-set timeout applies to both probes; the period is raised to at least the timeout so probes never overlap.
        ready_timeout = health_check_timeout_seconds or 3
        live_timeout = health_check_timeout_seconds or 5
        probes: dict[str, Any] = {
            "readinessProbe": {
                "httpGet": {"path": health_check_path, "port": port},
                "initialDelaySeconds": 5,
                "periodSeconds": max(10, ready_timeout),
                "timeoutSeconds": ready_timeout,
                "failureThreshold": 3,
            },
            "livenessProbe": {
                "httpGet": {"path": health_check_path, "port": port},
                "initialDelaySeconds": 30,
                "periodSeconds": max(20, live_timeout),
                "timeoutSeconds": live_timeout,
                "failureThreshold": 3,
            },
        }
    else:
        probes = {
            "readinessProbe": {
                "tcpSocket": {"port": port},
                "initialDelaySeconds": 2,
                "periodSeconds": 5,
            }
        }
    # Conventional PORT env var. An explicit container `env` entry beats `envFrom`, so only add it
    # when the user's own env doesn't define PORT (user value wins).
    port_env: dict[str, Any] = (
        {} if "PORT" in (user_env or {}) else {"env": [{"name": "PORT", "value": str(port)}]}
    )
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace_name(app_id),
            # The tag the user asked for (the container runs the pinned digest); display only.
            **({"annotations": {"picopaas.io/image-ref": image_ref}} if image_ref and image_ref != image else {}),
        },
        "spec": {
            "replicas": replicas,
            # Surge one pod (headroom is in app-quota), never drop below the running pod.
            # A volume app uses Recreate: a rolling surge pod could never attach the ReadWriteOnce volume (deadlock).
            "strategy": (
                {"type": "Recreate"} if volume
                else {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}
            ),
            "selector": {"matchLabels": pod_labels},
            "template": {
                "metadata": {"labels": pod_labels, "annotations": pod_annotations},
                "spec": {
                    "automountServiceAccountToken": False,
                    # Don't inject the {SVCNAME}_SERVICE_HOST/_PORT env vars for every Service in the namespace.
                    # Nothing in the app needs them, and they leak the namespace's service topology into the
                    # tenant's process env (CIS/pod-hardening best practice). The kubelet still injects the
                    # KUBERNETES_* vars for the API server, but there's no token to use them and egress to the
                    # API server's (private) IP is denied by the default-deny NetworkPolicy.
                    "enableServiceLinks": False,
                    "securityContext": {
                        "runAsNonRoot": RUN_AS_NON_ROOT,
                        "seccompProfile": {"type": "RuntimeDefault"},
                        # Lets the non-root UID write a fresh volume; OnRootMismatch skips the slow recursive chown on reattach.
                        **({"fsGroup": VOLUME_FS_GROUP, "fsGroupChangePolicy": "OnRootMismatch"} if volume else {}),
                    },
                    **(
                        {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc_name(app_id)}}]}
                        if volume else {}
                    ),
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "ports": [{"containerPort": port}],
                            "resources": {
                                "requests": {"cpu": f"{e['cpu_request_m']}m", "memory": f"{e['mem_request_mi']}Mi",
                                             "ephemeral-storage": LIMIT_RANGE_DEFAULT_REQUEST_EPHEMERAL},
                                "limits": {"cpu": f"{e['cpu_limit_m']}m", "memory": f"{e['mem_limit_mi']}Mi",
                                           "ephemeral-storage": f"{_eph(e)}Mi"},
                            },
                            "envFrom": [{"secretRef": {"name": _secret_name(app_id)}}],
                            "securityContext": {
                                "runAsNonRoot": RUN_AS_NON_ROOT,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"},
                                **(
                                    {"runAsUser": run_as_uid, "runAsGroup": run_as_uid}
                                    if run_as_uid is not None
                                    else {}
                                ),
                            },
                            **(
                                {"volumeMounts": [{"name": "data", "mountPath": volume["mount_path"]}]}
                                if volume else {}
                            ),
                            **port_env,
                            **probes,
                        }
                    ],
                },
            },
        },
    }


def pvc_manifest(app_id: int, volume: dict[str, Any]) -> dict[str, Any]:
    """The app's one persistent volume claim. Never deleted by reconcile/restart; goes with the namespace on app delete
    (StorageClass reclaimPolicy Delete). Size only ever grows (volume expansion)."""
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name(app_id), "namespace": namespace_name(app_id),
                     "labels": {"app": _resource_name(app_id)}},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": VOLUME_STORAGE_CLASS,
            "resources": {"requests": {"storage": f"{int(volume['size_gi'])}Gi"}},
        },
    }


def service_manifest(app_id: int, port: int) -> dict[str, Any]:
    name = _resource_name(app_id)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace_name(app_id)},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": name},
            "ports": [{"port": port, "targetPort": port, "protocol": "TCP"}],
        },
    }


def ingress_manifest(app_id: int, hostname: str, port: int, platform_subdomain: str | None = None) -> dict[str, Any]:
    name = _resource_name(app_id)
    full_host = _full_hostname(hostname)
    hosts = [full_host] + ([_full_hostname(platform_subdomain)] if platform_subdomain else [])
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": name, "namespace": namespace_name(app_id)},
        "spec": {
            "ingressClassName": INGRESS_CLASS,
            # No `tls[].secretName`: Traefik's default certificate (TLSStore `default`)
            # (the *.picopaas.xyz wildcard, cert-manager/DNS-01 — ARCHITECTURE.md "DNS
            # and TLS") serves every host automatically, so there's nothing per-app
            # to issue or copy into this namespace.
            "tls": [{"hosts": hosts}],
            "rules": [
                {
                    "host": h,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {"name": name, "port": {"number": port}}
                                },
                            }
                        ]
                    },
                }
                for h in hosts
            ],
        },
    }


def custom_ingress_name(app_id: int) -> str:
    return f"{_resource_name(app_id)}-custom"


def _custom_tls_secret_name(app_id: int) -> str:
    return f"{_resource_name(app_id)}-custom-tls"


def custom_domain_ingress_manifest(app_id: int, custom_host: str, port: int) -> dict[str, Any]:
    """An extra Ingress for a verified custom host. The cert-manager.io/cluster-issuer annotation makes
    cert-manager's ingress-shim issue a per-host cert (HTTP-01) into its own Secret, owned by this Ingress — so
    deleting this Ingress garbage-collects the Certificate and Secret too (custom-domain teardown)."""
    name = custom_ingress_name(app_id)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": name,
            "namespace": namespace_name(app_id),
            "annotations": {"cert-manager.io/cluster-issuer": CUSTOM_DOMAIN_ISSUER},
        },
        "spec": {
            "ingressClassName": INGRESS_CLASS,
            "tls": [{"hosts": [custom_host], "secretName": _custom_tls_secret_name(app_id)}],
            "rules": [
                {
                    "host": custom_host,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": _resource_name(app_id), "port": {"number": port}}},
                            }
                        ]
                    },
                }
            ],
        },
    }


def render_app_manifests(
    app_id: int,
    hostname: str,
    image: str,
    port: int,
    env: dict[str, str],
    mysql_cidr: str | None,
    replicas: int = 1,
    health_check_enabled: bool = False,
    health_check_path: str = "/healthz",
    mysql_port: int | None = None,
    storage_ips: list[str] | None = None,
    resources: dict[str, int] | None = None,
    include_ingress: bool = True,
    run_as_uid: int | None = None,
    restart_nonce: str | None = None,
    used_limits: dict[str, int] | None = None,
    image_ref: str | None = None,
    custom_host: str | None = None,
    platform_subdomain: str | None = None,
    health_check_timeout_seconds: int | None = None,
    volume: dict[str, Any] | None = None,
    volume_backup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render every manifest for one app (ingress is None when include_ingress is False, e.g. suspended). Pure: takes already-resolved data, returns dicts. `custom_host` adds a second Ingress (+ cert) for a verified custom domain."""
    return {
        "namespace": namespace_manifest(app_id),
        "role_binding": role_binding_manifest(app_id),
        "resource_quota": resource_quota_manifest(app_id, resources, used_limits, custom_domain=bool(custom_host),
                                                  replicas=replicas, volume=volume,
                                                  backup_pod=volume_backup["pod"] if volume_backup else None),
        "limit_range": limit_range_manifest(app_id, resources),
        "network_policies": network_policies(app_id, port, mysql_cidr, mysql_port, storage_ips,
                                              custom_domain=bool(custom_host), replicas=replicas,
                                              backup_ips=list(volume_backup["ips"]) if volume_backup else None),
        "secret": secret_manifest(app_id, env),
        "deployment": deployment_manifest(app_id, image, port, env_checksum=_env_checksum(env),
                                       replicas=replicas,
                                       health_check_enabled=health_check_enabled,
                                       health_check_path=health_check_path, user_env=env,
                                       resources=resources, run_as_uid=run_as_uid,
                                       restart_nonce=restart_nonce, image_ref=image_ref,
                                       health_check_timeout_seconds=health_check_timeout_seconds,
                                       volume=volume),
        "pvc": pvc_manifest(app_id, volume) if volume else None,
        "service": service_manifest(app_id, port),
        "ingress": ingress_manifest(app_id, hostname, port, platform_subdomain) if include_ingress else None,
        "custom_ingress": (
            custom_domain_ingress_manifest(app_id, custom_host, port)
            if include_ingress and custom_host else None
        ),
    }


def flatten_manifests(rendered: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten `render_app_manifests`'s output into one list, in apply order."""
    flat = [
        rendered["namespace"],
        rendered["role_binding"],
        rendered["resource_quota"],
        rendered["limit_range"],
        *rendered["network_policies"],
        rendered["secret"],
    ]
    if rendered.get("pvc") is not None:
        flat.append(rendered["pvc"])  # after the quota (so a grow fits), before the Deployment that mounts it
    flat += [
        rendered["deployment"],
        rendered["service"],
    ]
    if rendered.get("ingress") is not None:
        flat.append(rendered["ingress"])
    if rendered.get("custom_ingress") is not None:
        flat.append(rendered["custom_ingress"])
    return flat


def ingress_name(app_id: int) -> str:
    return _resource_name(app_id)
