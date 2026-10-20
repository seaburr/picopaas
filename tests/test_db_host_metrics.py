"""DO managed-DB host metrics: parser, fetch (mock transport), and Health fallback."""
import httpx
import pytest

from app import db_host_metrics as hm
from app import health

SAMPLE = '''# HELP cpu_usage_idle Telegraf collected metric
# TYPE cpu_usage_idle gauge
cpu_usage_idle{cpu="cpu-total",host="db-1",service="db"} 62.5
cpu_usage_idle{cpu="cpu0",host="db-1",service="db"} 10
# TYPE mem_total gauge
mem_total{host="db-1"} 1000
mem_available{host="db-1"} 250
mem_used_percent{host="db-1"} 40
disk_used_percent{host="db-1",path="/"} 12.25
disk_used_percent{host="db-1",path="/data"} 55.5
'''


def test_compute_sample():
    assert hm.compute(SAMPLE) == {"cpu_pct": 37.5, "mem_pct": 75.0, "disk_pct": 55.5}


def test_compute_fallbacks_and_missing():
    t = 'mem_used_percent 41.0\ndisk_used{path="/"} 25\ndisk_total{path="/"} 100\n'
    assert hm.compute(t) == {"cpu_pct": None, "mem_pct": 41.0, "disk_pct": 25.0}
    assert hm.compute("garbage") == {"cpu_pct": None, "mem_pct": None, "disk_pct": None}


def _transport(metrics_status=200):
    def h(req: httpx.Request):
        if req.url.host == "api.digitalocean.com":
            return httpx.Response(200, json={"credentials": {"basic_auth_username": "u", "basic_auth_password": "not-the-password"}})
        assert req.url.port == 9273 and req.url.path == "/metrics" and req.headers["authorization"].startswith("Basic ")
        return httpx.Response(metrics_status, text=SAMPLE)
    return httpx.MockTransport(h)


def test_fetch_ok_and_private_prefix_ok():
    hm.reset_credentials()
    assert hm.fetch_host_metrics("db.example", token="t", transport=_transport())["disk_pct"] == 55.5


def test_fetch_errors_scrubbed():
    hm.reset_credentials()
    with pytest.raises(hm.HostMetricsError, match="HTTP 500"):
        hm.fetch_host_metrics("db.example", token="t", transport=_transport(500))
    with pytest.raises(hm.HostMetricsError, match="no DIGITALOCEAN_TOKEN"):
        hm.fetch_host_metrics("db.example", token="")

    def boom(req):
        raise httpx.ConnectError("secret-host-detail")
    hm.reset_credentials()
    with pytest.raises(hm.HostMetricsError) as e:
        hm.fetch_host_metrics("db.example", token="t", transport=httpx.MockTransport(boom))
    assert "secret-host-detail" not in str(e.value)


def test_health_wrapper_fallbacks(monkeypatch):
    health.reset_cache()
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    assert health._host_metrics(1, "h") == {"host_metrics": None, "host_metrics_error": None}
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "x")

    def bad(host):
        raise RuntimeError("leak")
    monkeypatch.setattr(hm, "fetch_host_metrics", bad)
    out = health._host_metrics(1, "h")
    assert out["host_metrics"] is None and "leak" not in out["host_metrics_error"]
    health.reset_cache()
    monkeypatch.setattr(hm, "fetch_host_metrics", lambda host: {"cpu_pct": 1.0, "mem_pct": 2.0, "disk_pct": 3.0})
    assert health._host_metrics(1, "h")["host_metrics"]["disk_pct"] == 3.0
    health.reset_cache()
