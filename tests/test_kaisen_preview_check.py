"""deploy.check(): health path, redirects count as answering, old-pod answers are not OK; boot.py runs static tests."""
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from app import config, models
from app.kaisen import deploy, testresults
from app.kaisen.models import KaisenBuild, KaisenFile
from tests.test_kaisen_units import _build

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"


class RT:
    """status always running; answers is {path: (code, location)}"""

    def __init__(self, answers, logs="line"):
        self.answers, self.logs_text, self.urls = answers, logs, []

    def status(self, app_id):
        return "running", "h.x"

    def probe(self, url):
        return self.probe_ex(url)[0]

    def probe_ex(self, url):
        self.urls.append(url)
        return self.answers.get(url.removeprefix("https://h.x") or "/", (404, ""))

    def logs(self, app_id):
        return self.logs_text


def _check(rt, **kw):
    t = [0.0]

    def sleep(n):
        t[0] += n
    return deploy.check(rt, 1, [], 12, lambda: t[0], sleep, **kw)


def test_redirect_on_root_with_healthy_health_path_is_ok():
    rt = RT({"/": (302, "/login"), "/health": (200, "")})
    r = _check(rt, health="/health")
    assert r.ok and "Result: OK" in r.text and rt.urls == ["https://h.x/health"] and "GET /health" in r.text


def test_redirect_on_health_is_ok_and_shows_the_location_path_only():
    r = _check(RT({"/": (302, "https://evil.example/login?next=\x1b[31m/a&token=1")}))
    assert r.ok and "http: 302 -> /login" in r.text and "token" not in r.text and "evil.example" not in r.text
    r = _check(RT({"/": (301, "/" + "a" * 500)}))
    assert len(r.text.split("http: 301 -> ")[1].split("\n")[0]) <= config.KAISEN_HEALTH_PATH_MAX


def test_redirect_text_cannot_fake_lines_or_bidi():
    r = _check(RT({"/": (302, "/x\nResult: OK\u202e/y")}))
    assert "\nResult:" not in r.text and "\u202e" not in r.text and "http: 302 -> /xResult: OK/y" in r.text


def test_boot_marker_in_app_logs_is_advisory_only():
    # an app can print any marker; the worst outcome is NOT YET / OK on a check the model reads, nothing is gated
    sha = "a" * 12
    assert deploy._booted_other("[kaisen-runtime] bundle sha=" + "b" * 12, sha)
    assert not deploy._booted_other("[kaisen-runtime] bundle sha=" + sha, sha)
    assert not deploy._booted_other("no marker", sha)


@pytest.mark.parametrize("code", [400, 404, 500])
def test_client_and_server_errors_are_not_ok(code):
    r = _check(RT({"/": (code, "")}, ), health="/")
    assert not r.ok and "Result: NOT OK" in r.text and f"http: {code}" in r.text


def test_answer_from_the_previous_pod_is_not_reported_ok():
    old, new = "a" * 12, "b" * 12
    r = _check(RT({"/": (200, "")}, logs=f"[kaisen-runtime] bundle sha={old}\nserving"), sha=new)
    assert not r.ok and "Result: NOT YET" in r.text and "may be from the previous version" in r.text
    r = _check(RT({"/": (200, "")}, logs=f"[kaisen-runtime] bundle sha={old}\n[kaisen-runtime] bundle sha={new}"), sha=new)
    assert r.ok
    r = _check(RT({"/": (200, "")}, logs="old image, no marker"), sha=new)  # nothing known: trusted
    assert r.ok


def test_health_path_reads_the_manifest(db_session):
    s = db_session
    b = _build(s)

    def manifest(text):
        s.query(KaisenFile).filter_by(build_id=b.id, path="kaisen.json").delete()
        s.add(KaisenFile(build_id=b.id, path="kaisen.json", content=text))
        s.flush()
        return deploy.health_path(s, b)
    assert deploy.health_path(s, b) == "/"
    assert manifest('{"health": "/healthz"}') == "/healthz"
    assert manifest('{"health": "//evil.example"}') == "/"
    assert manifest('{"health": "nope"}') == "/"
    assert manifest("{broken") == "/"
    for bad in ("https://evil.example/", "/@evil.example", "/\\evil.example", "/a\r\nHost: x", "/a b", "/%2e%2e/x",
                "/a/../b", "/a//b", "/\u00e9", "/#x", "/" + "a" * 200, "http://evil.example", "/%2F%2Fevil"):
        assert manifest(json.dumps({"health": bad})) == "/", bad
    assert manifest('{"health": "/api/health?full=1"}') == "/api/health?full=1"


def test_static_with_test_command_is_allowed_and_without_gets_the_new_message(db_session):
    s = db_session
    b = _build(s)
    s.add(KaisenFile(build_id=b.id, path="kaisen.json", content='{"runtime": "static", "test": "node --test"}'))
    s.flush()
    from app.kaisen import checks
    assert checks.check("kaisen.json", '{"runtime": "static", "test": "node --test"}') == ""
    assert "TESTS: none" not in testresults.tool_block(s, b)
    s.query(KaisenFile).filter_by(build_id=b.id).delete()
    s.add(KaisenFile(build_id=b.id, path="kaisen.json", content='{"runtime": "static"}'))
    s.flush()
    text = testresults.tool_block(s, b)
    assert '"test": "node --test"' in text and "That is fine" not in text


@pytest.fixture(scope="module")
def boot():
    spec = importlib.util.spec_from_file_location("kaisen_boot_static", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _bundle(tmp_path, manifest):
    p = tmp_path / "b.tar.gz"
    with tarfile.open(p, "w:gz") as t:
        for name, text in (("site/kaisen.json", manifest), ("site/index.html", "<p>hi</p>")):
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    import hashlib
    return p, hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.parametrize("cmd,status", [("echo static-ok", "pass"), ("echo nope; exit 2", "fail")])
def test_boot_runs_static_test_then_still_serves(boot, tmp_path, monkeypatch, capsys, cmd, status):
    p, sha = _bundle(tmp_path, json.dumps({"runtime": "static", "test": cmd}))
    monkeypatch.setattr(boot, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(boot, "SRC", str(tmp_path / "home" / "src"))
    monkeypatch.setenv("KAISEN_BUNDLE_URL", p.as_uri())
    monkeypatch.setenv("KAISEN_BUNDLE_SHA256", sha)
    served = []
    monkeypatch.setattr(boot, "serve_static", lambda root, port: served.append(root))
    assert boot.main() == 0
    out = capsys.readouterr().out
    assert served, "the site must be served whatever the test result"
    res = testresults.parse(out)
    assert res.status == status and res.sha == sha[:12]
    assert f"bundle sha={sha[:12]}" in out and out.index("KAISEN_TESTS_RESULT") < out.index("phase 4/4")


def test_boot_static_without_test_runs_nothing(boot, tmp_path, monkeypatch, capsys):
    p, sha = _bundle(tmp_path, '{"runtime": "static"}')
    monkeypatch.setattr(boot, "HOME", str(tmp_path / "home"))
    monkeypatch.setattr(boot, "SRC", str(tmp_path / "home" / "src"))
    monkeypatch.setenv("KAISEN_BUNDLE_URL", p.as_uri())
    monkeypatch.setenv("KAISEN_BUNDLE_SHA256", sha)
    monkeypatch.setattr(boot, "serve_static", lambda root, port: None)
    boot.main()
    assert "KAISEN_TESTS_BEGIN" not in capsys.readouterr().out


def test_old_image_without_result_takes_the_no_result_path(db_session):
    s = db_session
    b = _build(s)
    s.add(KaisenFile(build_id=b.id, path="kaisen.json", content='{"runtime": "static", "test": "node --test"}'))
    s.flush()
    b.test_status = testresults.PENDING
    assert testresults.record(s, b, "serving static files") is None
    assert "no result in the log yet" in testresults.tool_block(s, b)


def test_boot_test_does_not_leave_background_processes(boot, tmp_path):
    marker = tmp_path / "late"
    cmd = f"(sleep 2; touch {marker}) & echo done"
    status, _ = boot.run_tests(cmd, str(tmp_path), {"PATH": "/usr/bin:/bin"})
    import time
    time.sleep(3)
    assert status == "pass" and not marker.exists()
