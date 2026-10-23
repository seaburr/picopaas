import pytest

from app.routers import admin_docs
from tests.conftest import register_and_login


@pytest.fixture()
def admin(make_client):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


@pytest.fixture()
def user(make_client, admin):
    c = make_client()
    register_and_login(c, "user@example.com")
    return c


def test_admin_guide_authz_and_content(admin, user, client, monkeypatch, tmp_path):
    guide = tmp_path / "ADMIN_GUIDE.md"
    guide.write_text("# Admin guide\n\nhello\n", encoding="utf-8")
    monkeypatch.setattr(admin_docs, "ADMIN_GUIDE_PATH", guide)
    r = admin.get("/admin/docs/admin-guide")
    assert r.status_code == 200 and r.json() == {"markdown": "# Admin guide\n\nhello\n"}
    assert user.get("/admin/docs/admin-guide").status_code == 403
    assert client.get("/admin/docs/admin-guide").status_code == 401


def test_admin_guide_missing_is_404(admin, monkeypatch, tmp_path):
    monkeypatch.setattr(admin_docs, "ADMIN_GUIDE_PATH", tmp_path / "nope.md")
    r = admin.get("/admin/docs/admin-guide")
    assert r.status_code == 404 and "ADMIN_GUIDE.md" in r.json()["detail"]
