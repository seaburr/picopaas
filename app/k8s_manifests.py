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
    EPHEMERAL_REQUEST_MI,
    INGRESS_CLASS,
    INGRESS_NAMESPACE,
)

RUN_AS_NON_ROOT = True

# ResourceQuota / LimitRange values (ARCHITECTURE.md §2 "Compute").
# The quota admits 2 pods' worth (2x the per-pod caps (app/config.py), which the LimitRange / container limits keep).
# Replicas are pinned to 1 by the platform and tenants can't create pods, so the extra headroom is only ever
# used by the surge pod during a rolling update (maxSurge 1); without it the surge pod is rejected with
# "exceeded quota: app-quota" and the rollout never completes.
POD_LIMIT_CPU = f"{DEFAULT_CPU_LIMIT_M}m"  # per-pod caps (what the health page shows)
POD_LIMIT_MEMORY = f"{DEFAULT_MEM_LIMIT_MI}Mi"
POD_LIMIT_EPHEMERAL = f"{EPHEMERAL_LIMIT_MI}Mi"
QUOTA_PODS = "2"
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
    "169.254.0.0/16",
]

MYSQL_PORT = 3306


def namespace_name(app_id: int) -> str:
    return f"app-{app_id}"


def _resource_name(app_id: int) -> str:
    return f"app-{app_id}"


def _secret_name(app_id: int) -> str:
    return f"app-{app_id}-env"


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


def resource_quota_manifest(
    app_id: int, resources: dict[str, int] | None = None, used_limits: dict[str, int] | None = None
) -> dict[str, Any]:
    """Quota = max(2 x new limits, limits already used by existing pods + new limits): the surge pod of a rollout
    runs the NEW limits while the old pod still consumes its own (possibly bigger) limits. `used_limits`
    ({"cpu_limit_m", "mem_limit_mi", "ephemeral_limit_mi", "pods"}, summed over the pods in the cluster) is None when there are no pods."""
    e = _res(resources)
    used = used_limits or {}
    cpu_total = max(2 * e["cpu_limit_m"], (used.get("cpu_limit_m") or 0) + e["cpu_limit_m"])
    mem_total = max(2 * e["mem_limit_mi"], (used.get("mem_limit_mi") or 0) + e["mem_limit_mi"])
    eph_total = max(2 * EPHEMERAL_LIMIT_MI, (used.get("ephemeral_limit_mi") or 0) + EPHEMERAL_LIMIT_MI)
    # Pod count too: a Terminating old pod still counts, so back-to-back rollouts (e.g. storage key swap then
    # recovery roll) need (existing + 1) slots, not a fixed 2.
    pods_total = max(int(QUOTA_PODS), (used.get("pods") or 0) + 1)
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "app-quota", "namespace": namespace_name(app_id)},
        "spec": {
            "hard": {
                "limits.cpu": f"{cpu_total}m",
                "limits.memory": f"{mem_total}Mi",
                "requests.cpu": f"{cpu_total}m",
                "requests.memory": f"{mem_total}Mi",
                "limits.ephemeral-storage": f"{eph_total}Mi",
                "requests.ephemeral-storage": f"{eph_total}Mi",
                "pods": str(pods_total),
                "services": QUOTA_SERVICES,
                # No LoadBalancer/NodePort services — the ingress controller (Traefik) is the only entry point
                # (ARCHITECTURE.md's no-LB lean option).
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
                            "ephemeral-storage": POD_LIMIT_EPHEMERAL},
                    "default": {"cpu": f"{e['cpu_limit_m']}m", "memory": f"{e['mem_limit_mi']}Mi",
                                "ephemeral-storage": POD_LIMIT_EPHEMERAL},
                    "defaultRequest": {"cpu": f"{e['cpu_request_m']}m", "memory": f"{e['mem_request_mi']}Mi",
                                       "ephemeral-storage": LIMIT_RANGE_DEFAULT_REQUEST_EPHEMERAL},
                }
            ]
        },
    }


SMTP_PORTS = (25, 465, 587)


def _internet_egress_ports() -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    start = 1
    for blocked in SMTP_PORTS:
        if blocked > start:
            ports.append({"protocol": "TCP", "port": start, "endPort": blocked - 1})
        elif blocked == start:
            pass
        start = blocked + 1
    ports.append({"protocol": "TCP", "port": start, "endPort": 65535})
    ports.append({"protocol": "UDP", "port": 1, "endPort": 65535})
    return ports


def network_policies(
    app_id: int,
    app_port: int,
    mysql_cidr: str | None,
    mysql_port: int | None = None,
    storage_ips: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Default-deny plus narrow allow rules.

    Kubernetes NetworkPolicy semantics are additive per matched pod: the
    "default-deny" policy below isolates every pod in the namespace for both
    Ingress and Egress (empty rule lists), and each "allow-*" policy adds one
    specific allowance on top. The union across all four is what actually
    takes effect.
    """
    namespace = namespace_name(app_id)
    all_pods_selector: dict[str, Any] = {"podSelector": {}}

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
            **all_pods_selector,
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
            **all_pods_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": list(PRIVATE_CIDRS),
                            }
                        }
                    ],
                    # NetworkPolicy can't deny, so allow everything EXCEPT SMTP
                    # (TCP 25/465/587) via endPort ranges (K8s >= 1.25). All UDP allowed.
                    "ports": _internet_egress_ports(),
                }
            ],
        },
    }

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
            **all_pods_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [mysql_peer],
                    "ports": [{"protocol": "TCP", "port": mysql_port or MYSQL_PORT}],
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
    ]
    if storage_ips:
        # Spaces resolves to a private VPC IP inside DO; the internet rule excludes PRIVATE_CIDRS,
        # so allow exactly those /32s on 443 (never the whole VPC range).
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-egress-storage", "namespace": namespace},
            "spec": {
                **all_pods_selector,
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"ipBlock": {"cidr": f"{ip}/32"}} for ip in sorted(set(storage_ips))],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                ],
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
) -> dict[str, Any]:
    e = _res(resources)
    name = _resource_name(app_id)
    pod_labels = {"app": name}
    # Changing env (e.g. rotated creds) changes this annotation, which rolls the pod;
    # envFrom alone never restarts a running pod.
    pod_annotations = {"picopaas.io/env-checksum": env_checksum} if env_checksum else {}
    if restart_nonce:  # POST /apps/{id}/restart: a new nonce changes the pod template, so the pod rolls
        pod_annotations["picopaas.io/restart-nonce"] = restart_nonce
    if health_check_enabled:
        probes: dict[str, Any] = {
            "readinessProbe": {
                "httpGet": {"path": health_check_path, "port": port},
                "initialDelaySeconds": 5,
                "periodSeconds": 10,
                "timeoutSeconds": 3,
                "failureThreshold": 3,
            },
            "livenessProbe": {
                "httpGet": {"path": health_check_path, "port": port},
                "initialDelaySeconds": 30,
                "periodSeconds": 20,
                "timeoutSeconds": 5,
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
    # Heroku-style PORT. An explicit container `env` entry beats `envFrom`, so only add it
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
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
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
                    },
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "ports": [{"containerPort": port}],
                            "resources": {
                                "requests": {"cpu": f"{e['cpu_request_m']}m", "memory": f"{e['mem_request_mi']}Mi",
                                             "ephemeral-storage": LIMIT_RANGE_DEFAULT_REQUEST_EPHEMERAL},
                                "limits": {"cpu": f"{e['cpu_limit_m']}m", "memory": f"{e['mem_limit_mi']}Mi",
                                           "ephemeral-storage": POD_LIMIT_EPHEMERAL},
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
                            **port_env,
                            **probes,
                        }
                    ],
                },
            },
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


def ingress_manifest(app_id: int, hostname: str, port: int) -> dict[str, Any]:
    name = _resource_name(app_id)
    full_host = _full_hostname(hostname)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": name, "namespace": namespace_name(app_id)},
        "spec": {
            "ingressClassName": INGRESS_CLASS,
            # No `tls[].secretName`: Traefik's default certificate (TLSStore `default`)
            # (the *.raisehellpraisedale.xyz wildcard, cert-manager/DNS-01 — ARCHITECTURE.md "DNS
            # and TLS") serves every host automatically, so there's nothing per-app
            # to issue or copy into this namespace.
            "tls": [{"hosts": [full_host]}],
            "rules": [
                {
                    "host": full_host,
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
) -> dict[str, Any]:
    """Render every manifest for one app (ingress is None when include_ingress is False, e.g. suspended). Pure: takes already-resolved data, returns dicts. `custom_host` adds a second Ingress (+ cert) for a verified custom domain."""
    return {
        "namespace": namespace_manifest(app_id),
        "role_binding": role_binding_manifest(app_id),
        "resource_quota": resource_quota_manifest(app_id, resources, used_limits),
        "limit_range": limit_range_manifest(app_id, resources),
        "network_policies": network_policies(app_id, port, mysql_cidr, mysql_port, storage_ips),
        "secret": secret_manifest(app_id, env),
        "deployment": deployment_manifest(app_id, image, port, env_checksum=_env_checksum(env),
                                       replicas=replicas,
                                       health_check_enabled=health_check_enabled,
                                       health_check_path=health_check_path, user_env=env,
                                       resources=resources, run_as_uid=run_as_uid,
                                       restart_nonce=restart_nonce, image_ref=image_ref),
        "service": service_manifest(app_id, port),
        "ingress": ingress_manifest(app_id, hostname, port) if include_ingress else None,
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
