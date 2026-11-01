"""Compatibility shim: DO host metrics moved to app/providers/digitalocean/db_metrics.py."""
from app.providers.digitalocean.db_metrics import (  # noqa: F401
    CRED_TTL_S, DO_API, WANTED, HostMetricsError, _cred, _cred_lock, _credentials, _verify, compute,
    fetch_host_metrics, parse_metrics, reset_credentials,
)
