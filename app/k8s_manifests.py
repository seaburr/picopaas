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

from typing import Any

from app.config import APP_DOMAIN

RUN_AS_NON_ROOT = True

# ResourceQuota / LimitRange values (ARCHITECTURE.md §2 "Compute").
QUOTA_LIMITS_CPU = "250m"
QUOTA_LIMITS_MEMORY = "512Mi"
QUOTA_REQUESTS_CPU = "250m"
QUOTA_REQUESTS_MEMORY = "512Mi"
QUOTA_PODS = "2"
QUOTA_SERVICES = "1"

LIMIT_RANGE_DEFAULT_CPU = "250m"
LIMIT_RANGE_DEFAULT_MEMORY = "512Mi"
LIMIT_RANGE_DEFAULT_REQUEST_CPU = "64m"
LIMIT_RANGE_DEFAULT_REQUEST_MEMORY = "128Mi"

INGRESS_NGINX_NAMESPACE = "ingress-nginx"
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
                "picopaas.io/app-id": str(app_id),
            },
        },
    }


def resource_quota_manifest(app_id: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "app-quota", "namespace": namespace_name(app_id)},
        "spec": {
            "hard": {
                "limits.cpu": QUOTA_LIMITS_CPU,
                "limits.memory": QUOTA_LIMITS_MEMORY,
                "requests.cpu": QUOTA_REQUESTS_CPU,
                "requests.memory": QUOTA_REQUESTS_MEMORY,
                "pods": QUOTA_PODS,
                "services": QUOTA_SERVICES,
                # No LoadBalancer/NodePort services — ingress-nginx is the only entry point
                # (ARCHITECTURE.md's no-LB lean option).
                "services.loadbalancers": "0",
                "services.nodeports": "0",
            }
        },
    }


def limit_range_manifest(app_id: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "app-limits", "namespace": namespace_name(app_id)},
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "default": {
                        "cpu": LIMIT_RANGE_DEFAULT_CPU,
                        "memory": LIMIT_RANGE_DEFAULT_MEMORY,
                    },
                    "defaultRequest": {
                        "cpu": LIMIT_RANGE_DEFAULT_REQUEST_CPU,
                        "memory": LIMIT_RANGE_DEFAULT_REQUEST_MEMORY,
                    },
                }
            ]
        },
    }


def network_policies(app_id: int, app_port: int, mysql_cidr: str) -> list[dict[str, Any]]:
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

    allow_ingress_nginx = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-ingress-nginx", "namespace": namespace},
        "spec": {
            **all_pods_selector,
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": INGRESS_NGINX_NAMESPACE
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
                    ]
                    # No `ports`: any protocol/port to the public internet is allowed.
                }
            ],
        },
    }

    allow_egress_mysql = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-egress-mysql", "namespace": namespace},
        "spec": {
            **all_pods_selector,
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": _to_cidr(mysql_cidr)}}],
                    "ports": [{"protocol": "TCP", "port": MYSQL_PORT}],
                }
            ],
        },
    }

    return [
        default_deny,
        allow_ingress_nginx,
        allow_egress_dns,
        allow_egress_internet,
        allow_egress_mysql,
    ]


def secret_manifest(app_id: int, env: dict[str, str]) -> dict[str, Any]:
    """`env` must already be resolved (see app/env_refs.py::resolve) — no `${{ }}` refs here."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": _secret_name(app_id), "namespace": namespace_name(app_id)},
        "type": "Opaque",
        "stringData": dict(env),
    }


def deployment_manifest(app_id: int, image: str, port: int) -> dict[str, Any]:
    name = _resource_name(app_id)
    pod_labels = {"app": name}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace_name(app_id)},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": pod_labels},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": RUN_AS_NON_ROOT,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "ports": [{"containerPort": port}],
                            "envFrom": [{"secretRef": {"name": _secret_name(app_id)}}],
                            "securityContext": {
                                "runAsNonRoot": RUN_AS_NON_ROOT,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "readinessProbe": {
                                "tcpSocket": {"port": port},
                                "initialDelaySeconds": 2,
                                "periodSeconds": 5,
                            },
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
            "ingressClassName": "nginx",
            # No `tls[].secretName`: ingress-nginx's configured default certificate
            # (the *.seaburr.io wildcard, cert-manager/DNS-01 — ARCHITECTURE.md "DNS
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


def render_app_manifests(
    app_id: int,
    hostname: str,
    image: str,
    port: int,
    env: dict[str, str],
    mysql_cidr: str,
) -> dict[str, Any]:
    """Render every manifest for one app. Pure: takes already-resolved data, returns dicts."""
    return {
        "namespace": namespace_manifest(app_id),
        "resource_quota": resource_quota_manifest(app_id),
        "limit_range": limit_range_manifest(app_id),
        "network_policies": network_policies(app_id, port, mysql_cidr),
        "secret": secret_manifest(app_id, env),
        "deployment": deployment_manifest(app_id, image, port),
        "service": service_manifest(app_id, port),
        "ingress": ingress_manifest(app_id, hostname, port),
    }


def flatten_manifests(rendered: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten `render_app_manifests`'s output into one list, in apply order."""
    flat = [
        rendered["namespace"],
        rendered["resource_quota"],
        rendered["limit_range"],
        *rendered["network_policies"],
        rendered["secret"],
        rendered["deployment"],
        rendered["service"],
        rendered["ingress"],
    ]
    return flat
