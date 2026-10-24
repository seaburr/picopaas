"""Security regressions for the 2026-10-24 feedback round: platform logs, admin settings, backup policy."""
import pytest

from app import backups, config, models, platform_settings
from app.dependencies import get_kube
from app.log_redact import redact_line
from app.main import app
from app.providers.fake import FakeKubeProvider
from tests.conftest import register_and_login


@pytest.fixture()
def kube():
    k = FakeKubeProvider()
    k.platform_pod_data[("picopaas-system", "api-0")] = {"containers": ["api"], "lines": {"api": [
        "GET /x Authorization: Bearer abcdefghijklmnop123",
        "db url mysql://user:not-the-password@host:3306/db",
        "FERNET_KEY=" + "A" * 43 + "=",
        "https://s.example/o?X-Amz-Signature=deadbeef&X-Amz-Expires=600",
        "plain line"]}}
    app.dependency_overrides[get_kube] = lambda: k
    yield k
    app.dependency_overrides.pop(get_kube, None)


def _admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


def _bearer(admin, scope):
    r = admin.post("/api/keys", json={"scope": scope})
    assert r.status_code in (200, 201), r.text
    return r.json()["key"] if "key" in r.json() else r.json()["token"]


Q = "namespace=picopaas-system&pod=api-0&container=api"


@pytest.mark.parametrize("scope", ["read", "full"])
def test_admin_api_key_cannot_read_platform_logs(make_client, kube, scope):
    admin = _admin(make_client)
    raw = _bearer(admin, scope)
    key_client = make_client()
    h = {"Authorization": f"Bearer {raw}"}
    for path in ("/admin/health/logs/pods", f"/admin/health/logs?{Q}", f"/admin/health/logs/download?{Q}"):
        assert key_client.get(path, headers=h).status_code == 403, path
    assert admin.get(f"/admin/health/logs?{Q}").status_code == 200  # the browser session still works


def test_read_key_cannot_change_settings_or_locks(make_client, kube):
    admin = _admin(make_client)
    raw = _bearer(admin, "read")
    c = make_client()
    h = {"Authorization": f"Bearer {raw}"}
    assert c.put("/admin/settings", json={"settings": {"max_replicas": 2}}, headers=h).status_code == 403
    assert c.put("/admin/apps/1/backups", json={"admin_disabled": False}, headers=h).status_code == 403


def test_log_lines_are_redacted_in_view_and_download(make_client, kube):
    admin = _admin(make_client)
    for text in (" ".join(admin.get(f"/admin/health/logs?{Q}").json()["lines"]), admin.get(f"/admin/health/logs/download?{Q}").text):
        for secret in ("abcdefghijklmnop123", "not-the-password", "A" * 43, "deadbeef"):
            assert secret not in text
        assert "plain line" in text and "X-Amz-Expires=600" in text


@pytest.mark.parametrize("line,secret", [
    ("cookie: session=abcdefghijklmnop.qrstu", "abcdefghijklmnop"),
    ('{"password": "not-the-password"}', "not-the-password"),
    ("token=sometokenvalue123", "sometokenvalue123"),
    ("key " + config.API_KEY_PREFIX + "0123456789abcdef0123456789abcdef", "0123456789abcdef"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_value_abc", "eyJzdWIiOiIxIn0"),
    ("blob gAAAAABnotarealfernettokenvalue12345==", "notarealfernettoken"),
])
def test_redact_line_patterns(line, secret):
    assert secret not in redact_line(line)


def test_redact_keeps_ordinary_lines():
    s = "2026-10-24T10:00:00Z INFO reconcile app 12 ok in 35ms"
    assert redact_line(s) == s


def test_internal_sweeper_row_is_not_a_writable_setting(make_client, db_session):
    admin = _admin(make_client)
    r = admin.put("/admin/settings", json={"settings": {"backup_sweeper_last": "{}"}})
    assert r.status_code == 422
    assert db_session.get(models.PlatformSetting, "backup_sweeper_last") is None
    keys = {s["key"] for g in admin.get("/admin/settings").json()["groups"] for s in g["settings"]}
    assert "backup_sweeper_last" not in keys


@pytest.mark.parametrize("value", [True, 1.5, "7", None, [3]])
def test_setting_type_confusion_rejected_or_reset(make_client, value):
    admin = _admin(make_client)
    r = admin.put("/admin/settings", json={"settings": {"max_replicas": value}})
    assert (r.status_code == 200) if value is None else (r.status_code == 422)


def test_every_int_setting_has_absolute_bounds():
    for s in platform_settings.REGISTRY:
        if s.type == "int":
            assert s.min is not None and s.max is not None, s.key


def test_non_admin_cannot_use_settings(make_client):
    _admin(make_client)
    c = make_client()
    register_and_login(c, "plain@example.com")
    assert c.get("/admin/settings").status_code == 403
    assert c.put("/admin/settings", json={"settings": {"max_replicas": 2}}).status_code == 403


def test_failure_text_hides_bucket_paths_and_traces(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_BUCKET", "picopaas-backups-xyz")
    out = backups.redact("cannot open s3:https://h/picopaas-backups-xyz/apps/1 at /work/repo/config; picopaas-backups-xyz denied")
    assert "picopaas-backups-xyz" not in out and "/work" not in out
    assert "Traceback" not in backups.redact("Traceback (most recent call last):\n  File \"/app/x.py\", line 1")
    assert backups.redact("the backups are kept") == "the backups are kept"
