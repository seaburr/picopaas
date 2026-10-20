"""Managed-MySQL *host* cpu/mem/disk % from DigitalOcean's scrapable metrics endpoint.

Endpoint: https://<db-host>:9273/metrics (basic auth). Credentials (account-wide) come from
GET https://api.digitalocean.com/v2/databases/metrics/credentials -> {"credentials": {"basic_auth_username",
"basic_auth_password"}} with DIGITALOCEAN_TOKEN. Docs:
https://www.digitalocean.com/community/developer-center/how-to-collect-scrapable-metrics-for-managed-dbaas-on-digitalocean
Metrics are Telegraf-style Prometheus text: cpu_usage_idle{cpu="cpu-total"}, mem_used_percent / mem_available /
mem_total, disk_used_percent / disk_used / disk_total. Everything is best-effort: missing -> None.
Nothing here logs or returns credentials or raw exception text.
"""
from __future__ import annotations

import os
import re
import ssl
import threading
import time
from typing import Any

import httpx

from app import config

DO_API = "https://api.digitalocean.com"
CRED_TTL_S = 3600
_cred: dict[str, Any] = {"at": None, "val": None}
_cred_lock = threading.Lock()

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([^\s]+)(?:\s+\d+)?\s*$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
WANTED = {"cpu_usage_idle", "cpu_usage_active", "mem_used_percent", "mem_available", "mem_total", "mem_used",
          "disk_used_percent", "disk_used", "disk_total"}


class HostMetricsError(Exception):
    """Message is already scrubbed (safe to show to an admin)."""


def parse_metrics(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for raw in text.splitlines():
        if not raw or raw[0] == "#":
            continue
        m = _LINE.match(raw.strip())
        if not m or m.group(1) not in WANTED:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if v != v:  # NaN
            continue
        labels = dict(_LABEL.findall(m.group(2) or ""))
        out.setdefault(m.group(1), []).append((labels, v))
    return out


def _pct(x: float | None) -> float | None:
    return None if x is None else round(max(0.0, min(100.0, x)), 1)


def compute(text: str) -> dict[str, float | None]:
    """OpenMetrics text -> {cpu_pct, mem_pct, disk_pct}; each None when its series are absent."""
    s = parse_metrics(text)
    cpu = None
    for labels, v in s.get("cpu_usage_idle", []):
        if labels.get("cpu", "cpu-total") == "cpu-total":
            cpu = 100.0 - v
    if cpu is None:
        for labels, v in s.get("cpu_usage_active", []):
            if labels.get("cpu", "cpu-total") == "cpu-total":
                cpu = v
    mem = None
    avail, total = s.get("mem_available"), s.get("mem_total")
    if avail and total and total[0][1] > 0:
        mem = 100.0 * (1 - avail[0][1] / total[0][1])  # excludes reclaimable cache, like `free`
    elif s.get("mem_used_percent"):
        mem = s["mem_used_percent"][0][1]
    disk = None
    # Several mounts may be reported; the data volume is the fullest one that matters, so take the max.
    pcts = [v for _, v in s.get("disk_used_percent", [])]
    if not pcts:
        tot = {tuple(sorted(l.items())): v for l, v in s.get("disk_total", [])}
        pcts = [100.0 * v / tot[k] for k, v in ((tuple(sorted(l.items())), v) for l, v in s.get("disk_used", []))
                if tot.get(k)]
    if pcts:
        disk = max(pcts)
    return {"cpu_pct": _pct(cpu), "mem_pct": _pct(mem), "disk_pct": _pct(disk)}


def _credentials(token: str, client: httpx.Client) -> tuple[str, str]:
    with _cred_lock:
        if _cred["val"] and time.monotonic() - _cred["at"] < CRED_TTL_S:
            return _cred["val"]
    r = client.get(f"{DO_API}/v2/databases/metrics/credentials", headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise HostMetricsError(f"DO metrics credentials request failed (HTTP {r.status_code})")
    c = r.json().get("credentials") or {}
    user, pw = c.get("basic_auth_username"), c.get("basic_auth_password")
    if not user or not pw:
        raise HostMetricsError("DO metrics credentials missing in response")
    with _cred_lock:
        _cred["at"], _cred["val"] = time.monotonic(), (user, pw)
    return user, pw


def reset_credentials() -> None:
    with _cred_lock:
        _cred["at"] = _cred["val"] = None


def _verify() -> Any:
    ca = config.DB_METRICS_CA or config.MYSQL_SSL_CA
    if ca and os.path.exists(ca):
        return ssl.create_default_context(cafile=ca)
    return True  # system CAs; never silently disabled


def fetch_host_metrics(host: str, *, token: str | None = None, transport: httpx.BaseTransport | None = None
                       ) -> dict[str, float | None]:
    """Scrape and compute. Raises HostMetricsError (scrubbed) on any failure."""
    token = token if token is not None else os.environ.get("DIGITALOCEAN_TOKEN", "")
    if not token:
        raise HostMetricsError("no DIGITALOCEAN_TOKEN")
    try:
        with httpx.Client(timeout=6.0, transport=transport) as api:
            user, pw = _credentials(token, api)
        # `private-` hostnames are VPC aliases; the metrics listener answers on the cluster's host name.
        hosts = [host] + ([host[len("private-"):]] if host.startswith("private-") else [])
        last: Exception | None = None
        for h in hosts:
            try:
                with httpx.Client(timeout=6.0, verify=_verify(), transport=transport) as c:
                    r = c.get(f"https://{h}:{config.DB_METRICS_PORT}/metrics", auth=(user, pw))
            except httpx.HTTPError as exc:
                last = exc
                continue
            if r.status_code == 401:
                reset_credentials()
                raise HostMetricsError("metrics endpoint rejected credentials (HTTP 401)")
            if r.status_code != 200:
                raise HostMetricsError(f"metrics endpoint returned HTTP {r.status_code}")
            return compute(r.text)
        raise HostMetricsError(f"metrics endpoint unreachable ({type(last).__name__})")
    except HostMetricsError:
        raise
    except Exception as exc:  # noqa: BLE001 - scrubbed: type only
        raise HostMetricsError(f"metrics fetch failed ({type(exc).__name__})") from None
