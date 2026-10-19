"""Per-app compute resources: platform defaults (bootstrap.md caps) and uberadmin overrides."""
from __future__ import annotations

from typing import Any

from app.config import (  # noqa: F401  (single source of truth for the caps)
    DEFAULT_CPU_LIMIT_M,
    DEFAULT_CPU_REQUEST_M,
    DEFAULT_MEM_LIMIT_MI,
    DEFAULT_MEM_REQUEST_MI,
)

CPU_MIN_M, CPU_MAX_M = 10, 1000
MEM_MIN_MI, MEM_MAX_MI = 32, 2048

FIELDS = ("cpu_request_m", "cpu_limit_m", "mem_request_mi", "mem_limit_mi")


def effective(app: Any) -> dict[str, int]:
    """Effective values for an App row (or None for all defaults). A request is clamped to the limit."""
    g = lambda name: getattr(app, name, None) if app is not None else None  # noqa: E731
    cpu_limit = g("cpu_limit_m") or DEFAULT_CPU_LIMIT_M
    mem_limit = g("mem_limit_mi") or DEFAULT_MEM_LIMIT_MI
    return {
        "cpu_request_m": min(g("cpu_request_m") or DEFAULT_CPU_REQUEST_M, cpu_limit),
        "cpu_limit_m": cpu_limit,
        "mem_request_mi": min(g("mem_request_mi") or DEFAULT_MEM_REQUEST_MI, mem_limit),
        "mem_limit_mi": mem_limit,
    }


def exceeds_caps(eff: dict[str, int]) -> bool:
    return eff["cpu_limit_m"] > DEFAULT_CPU_LIMIT_M or eff["mem_limit_mi"] > DEFAULT_MEM_LIMIT_MI


def overridden(app: Any) -> bool:
    return any(getattr(app, f, None) is not None for f in FIELDS)


def describe(app: Any) -> dict[str, Any]:
    eff = effective(app)
    return {**eff, "overridden": overridden(app), "exceeds_platform_caps": exceeds_caps(eff)}
