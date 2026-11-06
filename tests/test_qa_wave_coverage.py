"""QA coverage pass for the 2026-11-05 wave: resource arithmetic as pure units, both migrations on existing rows,
platform backup poll/finish failure paths. SQLite in-memory; infra through the fakes."""
import os
import subprocess
import sys
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

from app import config, models, platform_backup, resources
from app.providers.fake import FakeBackupRunner
from tests.conftest import utcnow_naive
from tests.test_backups import on  # noqa: F401


def _app(**kw):
    base = dict(cpu_limit_m=None, mem_limit_mi=None, ephemeral_limit_mi=None, storage_quota_mb=None, cpu_request_m=None,
                mem_request_mi=None, db_quota_mb=None, db_max_connections=None, image_size_mb=None, egress_mbit=None,
                owner_cpu_limit_m=None, owner_mem_limit_mi=None, owner_ephemeral_limit_mi=None, owner_storage_quota_mb=None,
                want_storage=True, storage=None, db_engine="mysql")
    base.update(kw)
    return SimpleNamespace(**base)


# ---- effective() = min(owner, ceiling) for every null / raised / lowered combination ----

@pytest.mark.parametrize("admin,owner,expected", [
    (None, None, config.DEFAULT_CPU_LIMIT_M),                 # both unset: platform default
    (None, 100, 100),                                         # owner lowered below default
    (None, 10_000, config.DEFAULT_CPU_LIMIT_M),               # owner "raised" past default: capped
    (500, None, 500),                                         # admin raised, owner unset: follows ceiling
    (500, 100, 100),                                          # admin raised, owner lowered
    (500, 400, 400),
    (500, 900, 500),                                          # owner above a later-lowered ceiling: ceiling wins
    (60, 100, 60),                                            # admin lowered below the owner's choice
    (60, 60, 60),
])
def test_effective_cpu_is_min_of_owner_and_ceiling(db_session, admin, owner, expected):
    a = _app(cpu_limit_m=admin, owner_cpu_limit_m=owner)
    assert resources.effective(a, db_session)["cpu_limit_m"] == expected
    assert resources.ceilings(a, db_session)["cpu_limit_m"] == (admin or config.DEFAULT_CPU_LIMIT_M)  # never lowered by owner


def test_every_owner_field_is_clamped_and_requests_follow_the_limit(db_session):
    a = _app(cpu_limit_m=400, mem_limit_mi=512, ephemeral_limit_mi=256, storage_quota_mb=80, cpu_request_m=300, mem_request_mi=300,
             owner_cpu_limit_m=100, owner_mem_limit_mi=128, owner_ephemeral_limit_mi=64, owner_storage_quota_mb=10)
    e = resources.effective(a, db_session)
    assert (e["cpu_limit_m"], e["mem_limit_mi"], e["ephemeral_limit_mi"], e["storage_quota_mb"]) == (100, 128, 64, 10)
    assert e["cpu_request_m"] <= 100 and e["mem_request_mi"] <= 128  # request never above the lowered limit
    assert resources.storage_quota_bytes(a, db_session) == 10 * 1024 * 1024
    assert resources.ephemeral_limit_mi(a, db_session) == 64
    # the ceilings (what the admin dialog and exceeds_caps see) ignore the owner entirely
    c = resources.ceilings(a, db_session)
    assert (c["cpu_limit_m"], c["mem_limit_mi"], c["ephemeral_limit_mi"], c["storage_quota_mb"]) == (400, 512, 256, 80)
    assert resources.exceeds_caps(c, db_session) is True
    assert resources.exceeds_caps(resources.ceilings(_app(owner_cpu_limit_m=60), db_session), db_session) is False


def test_storage_quota_bytes_owner_above_ceiling_is_capped(db_session):
    a = _app(storage_quota_mb=20, owner_storage_quota_mb=90)
    assert resources.storage_quota_bytes(a, db_session) == 20 * 1024 * 1024


def test_floor_is_capped_at_the_ceiling(db_session):
    low = _app(cpu_limit_m=20, mem_limit_mi=40, ephemeral_limit_mi=30)
    fl = resources.floors(resources.ceilings(low, db_session))
    assert fl["cpu_limit_m"] == 20 and fl["mem_limit_mi"] == 40 and fl["ephemeral_limit_mi"] == 30  # not 50 / 64 / 64
    normal = resources.floors(resources.ceilings(_app(), db_session))
    assert normal["cpu_limit_m"] == config.OWNER_CPU_MIN_M and normal["mem_limit_mi"] == config.OWNER_MEM_MIN_MI
    ctl = resources.controls(low, db_session)
    assert ctl["cpu_limit_m"]["min"] <= ctl["cpu_limit_m"]["max"] == 20


def test_validate_owner_changes_ranges_and_storage_rules(db_session):
    a = _app()
    err = resources.validate_owner_changes(a, {"cpu_limit_m": 1, "mem_limit_mi": 10_000, "storage_quota_mb": None,
                                                "ephemeral_limit_mi": None}, db_session)
    assert set(err) == {"cpu_limit_m", "mem_limit_mi"} and "between" in err["cpu_limit_m"]
    nobucket = _app(want_storage=False)
    assert "no storage bucket" in resources.validate_owner_changes(nobucket, {"storage_quota_mb": 5}, db_session)["storage_quota_mb"]
    assert resources.validate_owner_changes(nobucket, {"storage_quota_mb": None}, db_session) == {}  # reset is always fine
    used = _app(storage=SimpleNamespace(size_bytes=7 * 1024 * 1024 + 1))
    e = resources.validate_owner_changes(used, {"storage_quota_mb": 5}, db_session)["storage_quota_mb"]
    assert "using 8 MB" in e and "read-only" in e
    assert resources.validate_owner_changes(used, {"storage_quota_mb": 8}, db_session) == {}
    unmeasured = _app(storage=SimpleNamespace(size_bytes=None))
    assert resources.validate_owner_changes(unmeasured, {"storage_quota_mb": 5}, db_session) == {}


def test_set_owner_values_normalises_ceiling_to_null_and_reports_effective_change(db_session):
    a = _app(cpu_limit_m=300)
    assert resources.set_owner_values(a, {"cpu_limit_m": 300}, db_session) == (False, False)  # equal to ceiling -> NULL
    assert a.owner_cpu_limit_m is None
    assert resources.set_owner_values(a, {"cpu_limit_m": 100, "storage_quota_mb": 5}, db_session) == (True, True)
    assert a.owner_cpu_limit_m == 100 and a.owner_storage_quota_mb == 5
    assert resources.would_change(a, {"cpu_limit_m": 100}, db_session) is False
    assert resources.would_change(a, {"cpu_limit_m": None}, db_session) is True
    assert resources.would_change(a, {"cpu_limit_m": 9999}, db_session) is True  # >= ceiling means NULL, which differs from 100
    # an admin ceiling already below the owner's choice: changing the owner value is not an effective change
    capped = _app(cpu_limit_m=60, owner_cpu_limit_m=100)
    assert resources.set_owner_values(capped, {"cpu_limit_m": 40}, db_session)[0] is True
    assert resources.effective(capped, db_session)["cpu_limit_m"] == 40
    assert resources.set_owner_values(capped, {"cpu_limit_m": 80}, db_session)[0] is True   # back up to the ceiling
    assert "cpu_limit_m=None" in resources.owner_str(capped) and resources.effective(capped, db_session)["cpu_limit_m"] == 60


def test_describe_admin_values_are_raw_override_columns(db_session):
    a = _app(cpu_limit_m=None, owner_cpu_limit_m=70)
    d = resources.describe(a)
    assert d["admin_values"]["cpu_limit_m"] is None and d["cpu_limit_m"] == 70
    assert d["controls"]["cpu_limit_m"]["owner"] == 70 and d["controls"]["storage_quota_mb"]["owner"] is None
    assert resources.describe(_app(want_storage=False))["controls"]["storage_quota_mb"] is None


# ---- migrations on existing rows ----

def _alembic(url):
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": url, "ENVIRONMENT": "dev"}

    def run(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True,
                              capture_output=True, text=True).stdout
    return run


def test_owner_and_steward_migrations_keep_existing_rows_and_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path}/m.db"
    alembic = _alembic(url)
    alembic("upgrade", "a7f2c9d41b36")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO users (email, password_hash, role, status, app_limit, created_at, must_change_password) "
                       "VALUES ('u@example.com', 'x', 'user', 'active', 3, '2026-01-01', 0)"))
    cols = lambda t: {c["name"] for c in inspect(create_engine(url)).get_columns(t)}  # noqa: E731
    assert "theme" not in cols("users")
    alembic("upgrade", "c5e8a2d7b941")
    assert "package_steward" not in cols("kaisen_settings")
    with eng.begin() as c:
        c.execute(text("INSERT INTO kaisen_settings (project_id, model, effort, cap_run, cap_day, cap_build, web_access, reviewer, "
                       "updated_at) VALUES (7, 'm', 'low', 1, 2, 3, 0, 'on', '2026-01-01')"))
    assert set(resources.OWNER_COLUMNS.values()) <= cols("apps") and "theme" in cols("users")
    alembic("upgrade", "b3c8e5a1d7f4")
    assert "package_steward" in cols("kaisen_settings")
    with create_engine(url).connect() as c:
        assert c.execute(text("SELECT theme FROM users")).scalar() == "dark"  # existing user defaults to dark
        steward = c.execute(text("SELECT package_steward FROM kaisen_settings")).scalars().all()
        assert steward == ["on"]  # a pre-existing project's row reads "on" (server default)
    alembic("downgrade", "c5e8a2d7b941")
    assert "package_steward" not in cols("kaisen_settings") and "theme" in cols("users")
    alembic("downgrade", "a7f2c9d41b36")
    assert "theme" not in cols("users") and not (set(resources.OWNER_COLUMNS.values()) & cols("apps"))
    with create_engine(url).connect() as c:
        assert c.execute(text("SELECT count(*) FROM users")).scalar() == 1  # data survived the full round trip
    alembic("upgrade", "head")
    assert "package_steward" in cols("kaisen_settings")


# ---- platform backup: poll / finish failure paths ----

@pytest.fixture()
def pb(on, monkeypatch, db_session):  # noqa: F811
    monkeypatch.setattr(config, "DATABASE_URL", "mysql+pymysql://platform:not-the-db-pass@db.internal:25060/picopaas")
    return db_session


def _running(db, runner, t0):
    platform_backup.tick(db, runner, t0)  # due -> enqueue -> start
    run = db.query(models.PlatformBackupRun).one()
    assert run.status == "running"
    return run


def test_poll_failed_missing_and_overdue_jobs_are_finished_as_failures(pb):
    t0 = utcnow_naive()
    r = FakeBackupRunner()
    run = _running(pb, r, t0)
    r.fail(run.job_name, "password=not-the-password boom")
    platform_backup.tick(pb, r, t0 + timedelta(minutes=1))
    pb.refresh(run)
    assert run.status == "failed" and "not-the-password" not in run.error
    # next run: Job vanished
    t1 = t0 + timedelta(hours=2)
    platform_backup.tick(pb, r, t1)
    run2 = pb.query(models.PlatformBackupRun).order_by(models.PlatformBackupRun.id.desc()).first()
    assert run2.status == "running"
    r.jobs.clear()
    platform_backup.tick(pb, r, t1 + timedelta(minutes=1))
    pb.refresh(run2)
    assert run2.status == "failed" and run2.error
    # third: stuck past the deadline
    t2 = t1 + timedelta(hours=3)
    platform_backup.tick(pb, r, t2)
    run3 = pb.query(models.PlatformBackupRun).order_by(models.PlatformBackupRun.id.desc()).first()
    assert run3.status == "running"
    platform_backup.tick(pb, r, t2 + timedelta(seconds=config.BACKUP_JOB_DEADLINE_SECONDS + 600))
    pb.refresh(run3)
    assert run3.status == "failed" and "too long" in run3.error
    assert run3.job_name in r.cleaned


def test_poll_survives_a_status_exception_and_a_cleanup_exception(pb, monkeypatch):
    t0 = utcnow_naive()
    r = FakeBackupRunner()
    run = _running(pb, r, t0)
    with monkeypatch.context() as m:
        m.setattr(r, "status", lambda *a: (_ for _ in ()).throw(RuntimeError("api down")))
        platform_backup.tick(pb, r, t0 + timedelta(minutes=1))
    pb.refresh(run)
    assert run.status == "running"  # left alone, retried next tick
    r.finish(run.job_name)
    monkeypatch.setattr(r, "cleanup", lambda *a: (_ for _ in ()).throw(RuntimeError("gone")))
    platform_backup.tick(pb, r, t0 + timedelta(minutes=2))
    pb.refresh(run)
    assert run.status == "succeeded" and run.snapshot_id == "ab12cd34" and run.repo_bytes == 5000


def test_start_failures_finish_the_run_without_leaking_detail(pb):
    r = FakeBackupRunner()
    r.fail_start = True
    platform_backup.tick(pb, r, utcnow_naive())
    run = pb.query(models.PlatformBackupRun).one()
    assert run.status == "failed" and run.error == "the backup job could not be started"
    assert pb.query(models.AuditLog).filter_by(action="platform_backup.failed").count() == 1


def test_unreadable_password_fails_the_queued_run(pb):
    platform_backup._repo_password(pb)
    pb.get(models.PlatformSetting, platform_backup.PASSWORD_KEY).value = "garbage"
    pb.commit()
    platform_backup.tick(pb, FakeBackupRunner(), utcnow_naive())
    run = pb.query(models.PlatformBackupRun).one()
    assert run.status == "failed" and "can't be read" in run.error


def test_failed_scheduled_backup_retries_after_an_hour_not_the_interval(pb):
    t0 = utcnow_naive()
    r = FakeBackupRunner()
    run = _running(pb, r, t0)
    r.fail(run.job_name)
    platform_backup.tick(pb, r, t0 + timedelta(minutes=1))
    n = pb.query(models.PlatformBackupRun).count()
    platform_backup.tick(pb, r, t0 + timedelta(minutes=30))
    assert pb.query(models.PlatformBackupRun).count() == n  # inside the retry wait
    platform_backup.tick(pb, r, t0 + platform_backup.RETRY_AFTER_FAILURE + timedelta(minutes=2))
    assert pb.query(models.PlatformBackupRun).count() == n + 1


def test_export_failure_paths_do_not_touch_backup_fields(pb):
    now = utcnow_naive()
    src = models.PlatformBackupRun(trigger="scheduled", status="succeeded", created_at=now, finished_at=now, snapshot_id="aa11bb22",
                                   repo_bytes=500, bytes_added=9)
    pb.add(src)
    pb.commit()
    ex, created = platform_backup.enqueue_export(pb, src, now)
    assert created
    r = FakeBackupRunner()
    r.fail_start = True
    platform_backup.tick(pb, r, now)  # the schedule must not be due (fresh success) and the export fails to start
    pb.refresh(ex)
    assert ex.status == "failed" and ex.snapshot_id == "aa11bb22"
    assert pb.query(models.AuditLog).filter_by(action="platform_backup.export_failed").count() == 1
    assert pb.query(models.AuditLog).filter_by(action="platform_backup.failed").count() == 0
    # a snapshot that was pruned out from under a queued export fails it cleanly
    gone = models.PlatformBackupRun(trigger="export", status="queued", created_at=now, snapshot_id=None)
    pb.add(gone)
    pb.commit()
    platform_backup.tick(pb, FakeBackupRunner(), now)
    pb.refresh(gone)
    assert gone.status == "failed" and "gone" in gone.error
    assert src.bytes_added == 9 and src.repo_bytes == 500


# ---- pkgindex: range operators, empty documents, bad batch items ----

from app.kaisen import pkgindex  # noqa: E402


@pytest.mark.parametrize("spec,major,expected", [
    ("20", 20, True), ("20", 22, False), ("20.5", 20, False), ("=20", 20, True), ("=20.1.2", 20, False),
    (">20", 20, False), (">20", 21, True), (">20.1.0", 20, True), (">=20", 20, True), ("<20", 20, False), ("<20", 19, True),
    ("<=20", 20, True), ("<=20.1", 20, False), ("~20", 20, True), ("~20.1", 20, False), ("~20.1", 21, False),
    ("^20", 20, True), ("^20", 21, False), ("^0.2", 0, False), ("^0", 0, True),
    ("18 || 20", 20, True), ("18 || 20", 22, False), (">=18 <20", 19, True), (">=18 <20", 20, False),
    ("18 - 20", 19, True), ("18 - 20", 21, False), (">= 18", 18, True), ("*", 5, True), ("x", 5, True),
    ("lts/*", 5, True), ("x" * 5000, 5, True),
])
def test_node_range_operators(spec, major, expected):
    assert pkgindex.node_ok(spec, major) is expected


def test_registry_documents_with_nothing_usable_are_unknown():
    assert pkgindex.parse_npm({"versions": {"2.0.0-beta.1": {}}}) == {"status": "unknown"}
    assert pkgindex.parse_pypi({"releases": {"1.0rc1": [{}], "2.0.dev1": [{}]}}) == {"status": "unknown"}
    assert pkgindex.parse_npm("nope") == {"status": "unavailable"}
    assert pkgindex.parse_npm({"versions": {"1.0.0": "not-a-dict"}}) == {"status": "unknown"}


def test_non_dict_and_bad_ecosystem_items_are_invalid_and_good_ones_still_resolve():
    idx = pkgindex.PackageIndex(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=NPM_DOC_QA)))
    ticks = []
    out = idx.latest_versions([{"ecosystem": "npm", "name": "../x"}, {"ecosystem": "gem", "name": "rails"},
                               {"ecosystem": "npm", "name": "good"}], on_item=lambda: ticks.append(1))
    assert [o["status"] for o in out] == ["invalid", "invalid", "ok"] and ticks == [1]  # invalid items cost no lookup


NPM_DOC_QA = {"versions": {"1.0.0": {}, "1.1.0": {"engines": {"node": ">=18"}}}}


# ---- health statistics: Kaisen tables unavailable must not break the Health page ----

def test_statistics_falls_back_to_zero_when_the_kaisen_count_fails(db_session):
    from app import health
    db_session.add(models.User(email="s@example.com", password_hash="x", role="user", status="active", app_limit=3))
    db_session.commit()
    real_query = db_session.query
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # the Kaisen count
            raise RuntimeError("no such table")
        return real_query(*a, **k)
    db_session.query = flaky
    try:
        assert health.statistics(db_session) == {"apps_ever_deployed": 0, "kaisen_apps_ever_built": 0, "kaisen_runs_ever": 0}
    finally:
        del db_session.query
    assert health.statistics(db_session)["apps_ever_deployed"] == 0
