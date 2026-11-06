"""Admin > Health `statistics`: apps ever deployed and Kaisen apps ever built. Soft-deleted rows still count."""
import pytest

from app import health, models
from app.dependencies import get_kube
from app.kaisen.models import KaisenBuild, KaisenRun
from app.main import app
from app.providers.fake import FakeKubeProvider
from tests.conftest import register_and_login, utcnow_naive

APP = {"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}


@pytest.fixture()
def kube():
    health.reset_cache()
    app.dependency_overrides[get_kube] = lambda: FakeKubeProvider()
    yield
    health.reset_cache()


@pytest.fixture()
def admin(make_client, kube):
    c = make_client()
    register_and_login(c, "admin@example.com")
    return c


def _stats(c):
    r = c.get("/admin/health")
    assert r.status_code == 200, r.text
    return r.json()["statistics"]


def _build(db, n, runs=(), deleted=False):
    u = db.query(models.User).first()
    p = db.query(models.Project).first()
    if p is None:
        p = models.Project(name="P", slug="p-stat", created_by=u.id)
        db.add(p)
        db.flush()
    b = KaisenBuild(project_id=p.id, name=f"b{n}", slug=f"b{n}", brief="x", created_by=u.id,
                    deleted_at=utcnow_naive() if deleted else None)
    db.add(b)
    db.flush()
    for st in runs:
        db.add(KaisenRun(build_id=b.id, status=st))
    db.commit()
    return b


def test_empty_platform(admin):
    assert _stats(admin) == {"apps_ever_deployed": 0, "kaisen_apps_ever_built": 0}


def test_requires_admin(admin, make_client):
    u = make_client()
    register_and_login(u, "user@example.com")
    assert u.get("/admin/health").status_code == 403
    assert make_client().get("/admin/health").status_code in (401, 403)


def test_deleted_apps_still_count(admin, db_session):
    ids = [admin.post("/apps", json={**APP, "name": f"demo{i}"}).json()["id"] for i in range(3)]
    assert _stats(admin)["apps_ever_deployed"] == 3
    assert admin.delete(f"/apps/{ids[0]}").status_code == 204
    assert db_session.get(models.App, ids[0]).deleted_at is not None  # soft delete: the row stays
    s = admin.get("/admin/health").json()
    assert s["statistics"]["apps_ever_deployed"] == 3  # never goes down
    assert s["workloads"]["apps_total"] == 2  # the live count does


def test_kaisen_counts_builds_with_a_done_run_including_deleted(admin, db_session):
    admin.post("/apps", json=APP)  # makes the admin's personal project
    _build(db_session, 1, ["done"])
    _build(db_session, 2, ["error", "done", "done"])  # two done runs: one build
    _build(db_session, 3, ["error", "stopped", "running"])  # never finished
    _build(db_session, 4, [])  # never started
    _build(db_session, 5, ["done"], deleted=True)  # deleted build still counts
    assert _stats(admin)["kaisen_apps_ever_built"] == 3
    assert health.statistics(db_session)["kaisen_apps_ever_built"] == 3


def test_deleting_a_build_via_api_does_not_lower_the_count(admin, db_session):
    admin.post("/apps", json=APP)
    b = _build(db_session, 1, ["done"])
    before = _stats(admin)["kaisen_apps_ever_built"]
    db_session.query(KaisenBuild).filter_by(id=b.id).update({"deleted_at": utcnow_naive(), "app_id": None})
    db_session.commit()
    assert before == 1 and _stats(admin)["kaisen_apps_ever_built"] == 1
