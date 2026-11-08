"""Kaisen runtime bundle download: retries for a time budget, so a platform restart doesn't crash the app."""
import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"


@pytest.fixture()
def boot():
    spec = importlib.util.spec_from_file_location("kaisen_boot_download", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _unavailable(*_a, **_k):
    raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)


def test_keeps_retrying_through_a_two_minute_platform_outage(boot, monkeypatch, tmp_path):
    ft = FakeTime()
    calls = []

    def urlopen(url, timeout):
        calls.append(ft.t)
        if ft.t < 120:  # platform down for two minutes (node replacement)
            _unavailable()
        return io.BytesIO(b"bundle")

    monkeypatch.setattr(boot.urllib.request, "urlopen", urlopen)
    dest = tmp_path / "b.tgz"
    boot.download("http://x", str(dest), sleep=ft.sleep, clock=ft.clock)
    assert dest.read_bytes() == b"bundle"
    assert max(ft.sleeps) == boot.DOWNLOAD_MAX_DELAY_S
    assert calls[-1] - calls[-2] <= boot.DOWNLOAD_MAX_DELAY_S  # comes back within one capped wait


def test_gives_up_after_the_budget(boot, monkeypatch, tmp_path):
    ft = FakeTime()
    monkeypatch.setattr(boot.urllib.request, "urlopen", _unavailable)
    with pytest.raises(boot.BootError, match="in 300s"):
        boot.download("http://x", str(tmp_path / "b.tgz"), sleep=ft.sleep, clock=ft.clock)
    assert 240 < ft.t <= 300


def test_oversized_bundle_fails_at_once(boot, monkeypatch, tmp_path):
    ft = FakeTime()
    monkeypatch.setattr(boot, "MAX_BUNDLE_BYTES", 3)
    monkeypatch.setattr(boot.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"toolarge"))
    with pytest.raises(boot.BootError, match="larger than"):
        boot.download("http://x", str(tmp_path / "b.tgz"), sleep=ft.sleep, clock=ft.clock)
    assert ft.sleeps == []
