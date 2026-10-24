"""Backup sets (one backup = one package of db / blob storage / volume parts) and volume backups (kind "volume")."""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pytest
import yaml

from app import backups, config, k8s_manifests as k8s, models
from app.providers.backup import BackupJobSpec, job_manifests
from app.providers.fake import FakeKubeProvider, FakeMySQLAdmin, FakeObjectStore
from app.reconciler import reconcile_all
from tests.test_backups import (NOW, _add_storage, _server, admin, on, other, runner, user)  # noqa: F401
from tests.test_backups import _ghost_like, _sweep

VOL = {"size_gi": 1, "mount_path": "/data"}


def rows(db, action):
    db.expire_all()
    return db.query(models.AuditLog).filter_by(action=action).order_by(models.AuditLog.id).all()


@pytest.fixture()
def vols(monkeypatch):
    monkeypatch.setattr(config, "VOLUMES_ENABLED", True)


@pytest.fixture()
def scoped(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_KEY_SCOPED", True)


@pytest.fixture()
def api(runner):
    from app.main import app as fastapi_app
    from app.routers.backups import get_backup_runner
    fastapi_app.dependency_overrides[get_backup_runner] = lambda: runner
    yield
    fastapi_app.dependency_overrides.pop(get_backup_runner, None)


def _volume_app(client, db, name="blog", with_db=True, phase="Bound"):
    r = client.post("/apps", json={"name": name, "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": [],
                                   "want_db": with_db, "volume": VOL})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    if with_db:
        from app import crypto
        db.add(models.AppDatabase(app_id=aid, db_server_id=_server(db).id, db_name=f"app_{aid}", db_user=f"app_{aid}",
                                  password_encrypted=crypto.encrypt("not-the-db-password")))
    app = db.get(models.App, aid)
    app.volume_phase = phase
    db.commit()
    return aid


def _mk_set(db, aid, parts, trigger="manual", created=NOW, delete_status=None):
    """parts: kind -> status (or (status, snapshot, bytes, error))."""
    from app import crypto
    backups.ensure_config(db, db.get(models.App, aid), NOW)
    cfg = db.get(models.AppBackupConfig, aid)
    if cfg.repo_password_enc is None:
        cfg.repo_password_enc = crypto.encrypt("not-the-repo-password")
    cfg.next_backup_at = NOW + timedelta(days=30)
    st = models.BackupSet(app_id=aid, trigger=trigger, created_at=created, delete_status=delete_status)
    db.add(st)
    db.flush()
    ids = {}
    for kind, spec in parts.items():
        status, snap, nbytes, err = (spec, None, None, None) if isinstance(spec, str) else spec
        if status == "succeeded" and snap is None:
            snap = f"{kind[0]}0{kind[0]}0{kind[0]}0aa"[:8]
        r = models.BackupRun(app_id=aid, kind=kind, trigger=trigger, status=status, snapshot_id=snap, bytes_added=nbytes,
                             error=err, created_at=created, set_id=st.id,
                             finished_at=created + timedelta(seconds=30) if status in ("succeeded", "failed") else None)
        db.add(r)
        db.flush()
        ids[kind] = r.id
    db.commit()
    return st.id, ids


@pytest.fixture()
def app_ctx(on, vols, scoped, user, admin, db_session, api):
    aid = _volume_app(user, db_session)
    _add_storage(db_session, aid)
    db_session.get(models.App, aid).want_storage = True
    db_session.commit()
    return aid


# --- creation and listing ---------------------------------------------------------------------

def test_manual_backup_creates_one_set_with_a_part_per_kind(app_ctx, user, db_session):
    aid = app_ctx
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 202, r.text
    runs = r.json()
    assert [x["kind"] for x in runs] == ["db", "bucket", "volume"] and len({x["set_id"] for x in runs}) == 1
    st = db_session.query(models.BackupSet).one()
    assert (st.app_id, st.trigger, st.delete_status) == (aid, "manual", None)
    (a,) = rows(db_session, "backup.create")
    assert a.detail == f"trigger=manual set={st.id} kinds=db,bucket,volume"
    body = user.get(f"/apps/{aid}/backups").json()
    (b,) = body["backups"]
    assert b["id"] == st.id and b["trigger"] == "manual" and b["status"] == "running" and b["finished_at"] is None
    assert [p["kind"] for p in b["parts"]] == ["db", "bucket", "volume"] and all(p["status"] == "queued" for p in b["parts"])
    assert b["export"] is None and b["size_bytes"] == 0
    assert body["volume_backups"] == {"has_volume": True, "available": True, "note": None}
    assert len(body["runs"]) == 3  # the flat list keeps working


def test_scheduled_and_final_make_sets(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session)
    backups.tick(db_session, runner, NOW)
    st = db_session.query(models.BackupSet).one()
    assert st.trigger == "scheduled" and {r.kind for r in db_session.query(models.BackupRun)} >= {"db", "volume"}
    assert all(r.set_id == st.id for r in db_session.query(models.BackupRun).filter(models.BackupRun.trigger == "scheduled"))
    (a,) = rows(db_session, "backup.create")
    assert a.detail.startswith(f"trigger=scheduled set={st.id} kinds=")
    for s in list(runner.started):
        runner.finish(s.name)
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    user.delete(f"/apps/{aid}")
    _sweep(db_session, FakeKubeProvider())
    fin = db_session.query(models.BackupSet).filter_by(trigger="final").one()
    assert {r.kind for r in db_session.query(models.BackupRun).filter_by(set_id=fin.id)} == {"db", "volume"}


def test_set_status_matrix(app_ctx, user, db_session):
    aid = app_ctx
    ok = ("succeeded", None, 100, None)
    cases = {
        "running": ({"db": ok, "bucket": "running"}, "running"),
        "queued": ({"db": ok, "volume": "queued"}, "running"),
        "all ok": ({"db": ok, "bucket": ok, "volume": ok}, "succeeded"),
        "partial": ({"db": ok, "volume": ("failed", None, None, "some files on the volume could not be read")}, "partial"),
        "failed": ({"db": ("failed", None, None, "x"), "volume": ("failed", None, None, "y")}, "failed"),
    }
    expect = {}
    for i, (name, (parts, want)) in enumerate(cases.items()):
        sid, _ = _mk_set(db_session, aid, parts, created=NOW + timedelta(minutes=i))
        expect[sid] = want
    got = {b["id"]: b for b in user.get(f"/apps/{aid}/backups").json()["backups"]}
    assert {sid: got[sid]["status"] for sid in expect} == expect
    assert list(got) == sorted(got, reverse=True)  # newest first
    partial = next(b for b in got.values() if b["status"] == "partial")
    assert [(p["kind"], p["status"], p["error"]) for p in partial["parts"]] == [
        ("db", "succeeded", None), ("volume", "failed", "some files on the volume could not be read")]
    assert partial["size_bytes"] == 100 and partial["finished_at"] is not None
    assert next(b for b in got.values() if b["status"] == "running")["finished_at"] is None


def test_set_status_unit():
    st = models.BackupSet(app_id=1, trigger="manual")
    R = lambda s: models.BackupRun(app_id=1, kind="db", trigger="manual", status=s)  # noqa: E731
    assert backups.set_status(st, [R("succeeded"), R("failed")]) == "partial"
    assert backups.set_status(st, [R("deleting"), R("succeeded")]) == "deleting"
    st.delete_status = "deleting"
    assert backups.set_status(st, [R("succeeded")]) == "deleting"
    st.delete_status = "deleted"
    assert backups.set_status(st, [R("succeeded")]) == "deleted"
    st.delete_status = None
    assert backups.set_status(st, []) == "deleted"


def test_deleted_sets_are_hidden_and_other_users_get_404(app_ctx, user, other, db_session):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": "succeeded"}, delete_status="deleted")
    assert user.get(f"/apps/{aid}/backups").json()["backups"] == []
    live, _ = _mk_set(db_session, aid, {"db": "succeeded"})
    assert other.post(f"/apps/{aid}/backups/sets/{live}/download").status_code == 404
    assert other.delete(f"/apps/{aid}/backups/sets/{live}").status_code == 404
    assert user.post(f"/apps/{aid}/backups/sets/{sid}/download").status_code == 404
    assert user.delete(f"/apps/{aid}/backups/sets/{sid}").status_code == 404
    assert user.delete(f"/apps/{aid}/backups/sets/99999").status_code == 404


# --- migration --------------------------------------------------------------------------------

def test_migration_groups_existing_runs_into_sets(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/m.db", "ENVIRONMENT": "dev"}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True,
                              capture_output=True, text=True).stdout

    alembic("upgrade", "e5b1c7d9a2f4")
    con = sqlite3.connect(tmp_path / "m.db")
    con.execute("PRAGMA foreign_keys=OFF")
    ins = ("INSERT INTO backup_runs (app_id, kind, trigger, status, created_at) VALUES (?, ?, ?, 'succeeded', ?)")
    t0 = "2026-10-24 03:00:00.000000"
    for app_id, kind, trig, ts in [
        (1, "db", "scheduled", t0), (1, "bucket", "scheduled", "2026-10-24 03:00:02.000000"),  # one pair
        (1, "db", "scheduled", "2026-10-25 03:00:00.000000"),                                    # a lone run the next day
        (1, "db", "manual", "2026-10-25 03:00:01.000000"),                                       # other trigger: own set
        (2, "db", "final", t0),                                                                  # other app: own set
        (1, "db", "purge", t0), (1, "db", "restore", t0),                                        # not backups: no set
    ]:
        con.execute(ins, (app_id, kind, trig, ts))
    con.commit()
    con.close()
    alembic("upgrade", "head")
    con = sqlite3.connect(tmp_path / "m.db")
    sets = con.execute("SELECT id, app_id, trigger, created_at FROM backup_sets ORDER BY id").fetchall()
    assert sorted((s[1], s[2]) for s in sets) == [(1, "manual"), (1, "scheduled"), (1, "scheduled"), (2, "final")]
    by_run = {r[0]: r[1] for r in con.execute("SELECT id, set_id FROM backup_runs")}
    assert by_run[1] == by_run[2] and by_run[3] != by_run[1] and by_run[4] not in (by_run[1], by_run[3])
    assert by_run[5] not in (by_run[1], by_run[3], by_run[4])
    assert by_run[6] is None and by_run[7] is None
    alembic("downgrade", "e5b1c7d9a2f4")
    alembic("upgrade", "head")


# --- the package download ---------------------------------------------------------------------

def _ready(db, runner, user, aid, sid):
    r = user.post(f"/apps/{aid}/backups/sets/{sid}/download")
    assert r.status_code == 202, r.text
    ex = r.json()
    backups.tick(db, runner, datetime.utcnow())
    spec = runner.started[-1]
    runner.finish(spec.name, {"exported": True, "bytes": 4242})
    backups.tick(db, runner, datetime.utcnow())
    return ex, spec


def test_package_download_flow(app_ctx, user, db_session, runner):
    aid = app_ctx
    ok = ("succeeded", None, 100, None)
    sid, ids = _mk_set(db_session, aid, {"db": ok, "bucket": ok, "volume": ok})
    r = user.post(f"/apps/{aid}/backups/sets/{sid}/download")
    assert r.status_code == 202, r.text
    ex = r.json()
    assert (ex["set_id"], ex["kind"], ex["status"], ex["url"], ex["run_id"]) == (sid, "package", "queued", None, 0)
    assert ex["filename"] == f"blog-backup{sid}.tar.gz"
    listing = user.get(f"/apps/{aid}/backups").json()["backups"][0]
    assert listing["export"] == {"id": ex["export_id"], "status": "queued", "available_until": None}
    backups.tick(db_session, runner, datetime.utcnow())
    spec = runner.started[-1]
    env = spec.env
    assert env["MODE"] == "package" and env["REPO_BASE"].endswith(f"/backups/apps/{aid}") and "RESTIC_REPOSITORY" not in env
    assert env["PARTS"] == "db=d0d0d0aa,bucket=b0b0b0aa,volume=v0v0v0aa"
    assert env["SET_ID"] == str(sid) and env["APP_NAME"] == "blog" and env["PICOPAAS_VERSION"] == config.PICOPAAS_VERSION
    assert env["EXPORT_KEY"] == f"exports/{aid}/{ex['export_id']}.tar.gz" and env["EXPORT_BUCKET"] == "picopaas-backups"
    assert "DB_PASSWORD" not in env and "SRC_SECRET_KEY" not in env
    assert spec.kind == "package" and spec.namespace is None and spec.volume is None and spec.scratch_mib > 1000
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "running"
    runner.finish(spec.name, {"exported": True, "bytes": 4242})
    backups.tick(db_session, runner, datetime.utcnow())
    got = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert got["status"] == "ready" and got["size_bytes"] == 4242 and got["url"].startswith("https://")
    assert runner.presigned[-1] == (f"exports/{aid}/{ex['export_id']}.tar.gz", 600, f"blog-backup{sid}.tar.gz")
    got2 = user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()
    assert got2["url"] and len(runner.presigned) == 2  # a fresh presigned URL per call
    listing = user.get(f"/apps/{aid}/backups").json()["backups"][0]
    assert listing["export"]["status"] == "ready" and listing["export"]["available_until"]
    # idempotent while it is live
    again = user.post(f"/apps/{aid}/backups/sets/{sid}/download").json()
    assert again["export_id"] == ex["export_id"]
    (a,) = rows(db_session, "backup.download")
    assert a.detail == f"set={sid} kinds=db,bucket,volume export={ex['export_id']}" and "https://" not in a.detail
    links = rows(db_session, "backup.download_link")
    assert links and f"set={sid}" in links[0].detail and "sig=" not in links[0].detail


def test_package_has_only_the_succeeded_parts(app_ctx, user, db_session, runner):
    aid = app_ctx
    sid, ids = _mk_set(db_session, aid, {"db": "succeeded", "bucket": ("failed", None, None, "boom"), "volume": "succeeded"})
    user.post(f"/apps/{aid}/backups/sets/{sid}/download")
    backups.tick(db_session, runner, datetime.utcnow())
    kinds = [p.split("=")[0] for p in runner.started[-1].env["PARTS"].split(",")]
    assert kinds == ["db", "volume"]


def test_package_refusals(app_ctx, user, db_session, runner):
    aid = app_ctx
    running, _ = _mk_set(db_session, aid, {"db": "succeeded", "volume": "running"})
    r = user.post(f"/apps/{aid}/backups/sets/{running}/download")
    assert r.status_code == 409 and "still being taken" in r.json()["detail"]
    failed, _ = _mk_set(db_session, aid, {"db": ("failed", None, None, "x")})
    assert user.post(f"/apps/{aid}/backups/sets/{failed}/download").status_code == 422
    deleting, _ = _mk_set(db_session, aid, {"db": "succeeded"}, delete_status="deleting")
    r = user.post(f"/apps/{aid}/backups/sets/{deleting}/download")
    assert r.status_code == 404 and "can't find" in r.json()["detail"]


def test_package_limits_count_per_set_download(app_ctx, user, db_session, runner):
    aid = app_ctx
    sets = [_mk_set(db_session, aid, {"db": "succeeded"}, created=NOW + timedelta(minutes=i))[0] for i in range(4)]
    for sid in sets[:3]:
        ex, _ = _ready(db_session, runner, user, aid, sid)
    r = user.post(f"/apps/{aid}/backups/sets/{sets[3]}/download")
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_second_package_waits_for_the_first(app_ctx, user, db_session, runner):
    aid = app_ctx
    a, _ = _mk_set(db_session, aid, {"db": "succeeded"})
    b, _ = _mk_set(db_session, aid, {"db": "succeeded"}, created=NOW + timedelta(minutes=1))
    assert user.post(f"/apps/{aid}/backups/sets/{a}/download").status_code == 202
    r = user.post(f"/apps/{aid}/backups/sets/{b}/download")
    assert r.status_code == 409  # one download being prepared per app


def test_packages_run_one_at_a_time_platform_wide(app_ctx, user, db_session, runner, make_client):
    aid = app_ctx
    other_aid = _ghost_like(user, db_session, "devlog")
    from tests.test_backups import _succeeded  # noqa: F401
    a, _ = _mk_set(db_session, aid, {"db": "succeeded"})
    b, _ = _mk_set(db_session, other_aid, {"db": "succeeded"})
    user.post(f"/apps/{aid}/backups/sets/{a}/download")
    user.post(f"/apps/{other_aid}/backups/sets/{b}/download")
    backups.tick(db_session, runner, datetime.utcnow())
    assert [s.kind for s in runner.started] == ["package"]  # the second waits: a package's scratch is the biggest Job there is


def test_read_scope_key_can_list_but_not_download_or_delete(app_ctx, user, db_session, make_client):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": "succeeded"})
    key = user.post("/api/keys", json={"scope": "read"}).json()["key"]
    ro = make_client()
    ro.headers["Authorization"] = f"Bearer {key}"
    assert ro.get(f"/apps/{aid}/backups").status_code == 200
    assert ro.post(f"/apps/{aid}/backups/sets/{sid}/download").status_code == 403
    assert ro.delete(f"/apps/{aid}/backups/sets/{sid}").status_code == 403
    assert db_session.query(models.BackupRun).filter_by(trigger="export").count() == 0


# --- delete of a set --------------------------------------------------------------------------

def test_delete_set_forgets_every_part_then_hides_it(app_ctx, user, db_session, runner):
    aid = app_ctx
    sid, ids = _mk_set(db_session, aid, {"db": "succeeded", "bucket": ("failed", None, None, "x"), "volume": "succeeded"})
    r = user.delete(f"/apps/{aid}/backups/sets/{sid}")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["set_id"] == sid and body["status"] == "deleting" and len(body["delete_run_ids"]) == 2
    listing = user.get(f"/apps/{aid}/backups").json()["backups"][0]
    assert listing["status"] == "deleting"
    assert db_session.get(models.BackupRun, ids["bucket"]).status == "deleted"  # a failed part holds no snapshot
    (a,) = rows(db_session, "backup.delete")
    assert a.detail == f"set={sid} runs={ids['db']},{ids['volume']} kinds=db,volume"
    backups.tick(db_session, runner, datetime.utcnow())
    forgets = [s for s in runner.started if s.env["MODE"] == "forget"]
    assert {s.kind for s in forgets} == {"db", "volume"} and all(s.namespace is None and s.volume is None for s in forgets)
    for s in forgets:
        assert s.env["SNAPSHOT"] and s.env["RESTIC_REPOSITORY"].endswith(f"/apps/{aid}/{s.kind}")
        runner.finish(s.name, {"forgotten": True, "repo_bytes": 10})
    backups.tick(db_session, runner, datetime.utcnow())
    backups.tick(db_session, runner, datetime.utcnow())
    db_session.expire_all()
    assert db_session.get(models.BackupSet, sid).delete_status == "deleted"
    assert user.get(f"/apps/{aid}/backups").json()["backups"] == []
    assert {r.status for r in db_session.query(models.BackupRun).filter_by(set_id=sid, trigger="manual")} == {"deleted"}
    deleted = rows(db_session, "backup.deleted")
    assert len(deleted) == 2 and all(f"set={sid}" in d.detail for d in deleted)
    assert user.delete(f"/apps/{aid}/backups/sets/{sid}").status_code == 404


def test_delete_set_when_a_part_delete_fails_the_set_comes_back(app_ctx, user, db_session, runner):
    aid = app_ctx
    sid, ids = _mk_set(db_session, aid, {"db": "succeeded", "volume": "succeeded"})
    user.delete(f"/apps/{aid}/backups/sets/{sid}")
    backups.tick(db_session, runner, datetime.utcnow())
    by_kind = {s.kind: s for s in runner.started if s.env["MODE"] == "forget"}
    runner.finish(by_kind["db"].name, {"forgotten": True})
    runner.fail(by_kind["volume"].name, "deleting the backup failed")
    backups.tick(db_session, runner, datetime.utcnow())
    db_session.expire_all()
    st = db_session.get(models.BackupSet, sid)
    assert st.delete_status is None
    b = user.get(f"/apps/{aid}/backups").json()["backups"][0]
    assert b["status"] == "succeeded" and [p["kind"] for p in b["parts"]] == ["volume"]  # what could not be deleted is still there
    assert db_session.query(models.AppEvent).filter_by(kind="backup_failed").count() == 1
    assert rows(db_session, "backup.delete_failed")


def test_delete_set_refusals(app_ctx, user, db_session, runner):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": "succeeded", "volume": "running"})
    r = user.delete(f"/apps/{aid}/backups/sets/{sid}")
    assert r.status_code == 409 and "still being taken" in r.json()["detail"]
    ok, _ = _mk_set(db_session, aid, {"db": "succeeded"}, created=NOW + timedelta(minutes=5))
    assert user.post(f"/apps/{aid}/backups/sets/{ok}/download").status_code == 202
    r = user.delete(f"/apps/{aid}/backups/sets/{ok}")
    assert r.status_code == 409 and "download" in r.json()["detail"]


def test_delete_set_removes_a_ready_package(app_ctx, user, db_session, runner):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": "succeeded", "volume": "succeeded"})
    ex, _ = _ready(db_session, runner, user, aid, sid)
    r = user.delete(f"/apps/{aid}/backups/sets/{sid}")
    assert r.status_code == 202
    assert runner.deleted_exports == [f"exports/{aid}/{ex['export_id']}.tar.gz"]
    assert user.get(f"/apps/{aid}/backups/exports/{ex['export_id']}").json()["status"] == "expired"
    (a,) = rows(db_session, "backup.export_deleted")
    assert a.detail == f"export={ex['export_id']} set={sid} reason=backup_deleted"


def test_delete_set_of_only_failed_parts_is_immediate(app_ctx, user, db_session, runner):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": ("failed", None, None, "x")})
    r = user.delete(f"/apps/{aid}/backups/sets/{sid}")
    assert r.status_code == 202 and r.json() == {"set_id": sid, "status": "deleted", "delete_run_ids": []}
    assert user.get(f"/apps/{aid}/backups").json()["backups"] == []


# --- the volume gate --------------------------------------------------------------------------

def test_gate_off_leaves_the_volume_out_and_says_why(on, vols, user, db_session, runner):
    aid = _volume_app(user, db_session)
    assert config.BACKUP_KEY_SCOPED is False
    assert backups.eligible_kinds(db_session.get(models.App, aid)) == ["db"]
    body = user.get(f"/apps/{aid}/backups").json()
    assert body["volume_backups"] == {"has_volume": True, "available": False,
                                      "note": "Volume backups need a bucket-scoped backup key; ask the admin."}
    assert [k["kind"] for k in body["kinds"]] == ["db"]
    r = user.post(f"/apps/{aid}/backups", json={"kinds": ["volume"]})
    assert r.status_code == 422 and r.json()["detail"] == "Volume backups need a bucket-scoped backup key; ask the admin."
    r = user.post(f"/apps/{aid}/backups")  # no kinds: the db only, the volume is simply not included
    assert r.status_code == 202 and [x["kind"] for x in r.json()] == ["db"]
    backups.tick(db_session, runner, NOW)
    assert all(s.kind != "volume" and s.namespace is None for s in runner.started)


def test_gate_off_volume_only_app_cannot_back_up(on, vols, user, db_session):
    aid = _volume_app(user, db_session, with_db=False)
    r = user.post(f"/apps/{aid}/backups")
    assert r.status_code == 422 and "bucket-scoped backup key" in r.json()["detail"]


def test_gate_off_start_refuses_a_queued_volume_run(on, vols, scoped, user, db_session, runner, monkeypatch):
    aid = _volume_app(user, db_session, with_db=False)
    sid, ids = _mk_set(db_session, aid, {"volume": "queued"})
    monkeypatch.setattr(config, "BACKUP_KEY_SCOPED", False)  # flipped off between queueing and starting
    backups.tick(db_session, runner, NOW)
    run = db_session.get(models.BackupRun, ids["volume"])
    assert run.status == "failed" and "bucket-scoped" in run.error and runner.started == []


def test_gate_on_but_feature_unconfigured_is_inert(vols, scoped, user, db_session, runner, monkeypatch):
    monkeypatch.setattr(config, "BACKUPS_ENABLED", True)
    aid = _volume_app(user, db_session)
    assert not backups.volume_backups_on()
    assert backups.eligible_kinds(db_session.get(models.App, aid)) == ["db"]
    assert user.post(f"/apps/{aid}/backups").status_code == 503  # feature on but no bucket configured: nothing runs


# --- the volume Job ---------------------------------------------------------------------------

def test_volume_job_runs_in_the_app_namespace_with_the_manifest_hardening(app_ctx, user, db_session, runner):
    aid = app_ctx
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    # BACKUP_MAX_CONCURRENT = 2: finish the first two, the volume starts after
    for s in list(runner.started):
        runner.finish(s.name)
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    spec = next(s for s in runner.started if s.kind == "volume")
    assert spec.namespace == f"app-{aid}" and spec.volume and spec.scratch_mib == 0
    env = spec.env
    assert env["MODE"] == "backup" and env["KIND"] == "volume" and env["VOLUME_DIR"] == config.BACKUP_VOLUME_MOUNT
    assert env["RESTIC_REPOSITORY"].endswith(f"/apps/{aid}/volume") and env["LIMIT_UPLOAD_KIB"] == str(config.BACKUP_VOLUME_UPLOAD_LIMIT_KIB)
    assert "DB_PASSWORD" not in env and "SRC_SECRET_KEY" not in env  # only the backup key + repo password reach the tenant namespace
    secret, job = job_manifests(spec)
    pod = job["spec"]["template"]["spec"]
    assert secret["metadata"]["namespace"] == job["metadata"]["namespace"] == f"app-{aid}"
    assert pod["automountServiceAccountToken"] is False and "serviceAccountName" not in pod and pod["enableServiceLinks"] is False
    assert pod["restartPolicy"] == "Never" and job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == config.BACKUP_JOB_DEADLINE_SECONDS
    sc = pod["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["fsGroup"] == config.VOLUME_FS_GROUP and sc["runAsUser"] == config.VOLUME_FS_GROUP
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    (c,) = pod["containers"]
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    data = next(v for v in pod["volumes"] if v["name"] == "data")
    assert data["persistentVolumeClaim"] == {"claimName": k8s.pvc_name(aid), "readOnly": True}
    mount = next(m for m in c["volumeMounts"] if m["name"] == "data")
    assert mount == {"name": "data", "mountPath": config.BACKUP_VOLUME_MOUNT, "readOnly": True}
    assert pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"] == [
        {"labelSelector": {"matchLabels": {"app": f"app-{aid}"}}, "topologyKey": "kubernetes.io/hostname"}]
    # the container fits the namespace LimitRange (max = the app's own limits) and is small
    lim = c["resources"]["limits"]
    assert lim == {"cpu": f"{config.DEFAULT_CPU_LIMIT_M}m", "memory": "320Mi", "ephemeral-storage": f"{min(200, config.EPHEMERAL_LIMIT_MI)}Mi"}
    assert c["env"] == [{"name": "GOMEMLIMIT", "value": "256MiB"}]
    # the pod carries the backup label (the egress policy keys off it) and never the app's own `app` label
    assert job["spec"]["template"]["metadata"]["labels"]["picopaas.io/backup"] == "1"
    assert "app" not in job["spec"]["template"]["metadata"]["labels"]
    # a platform-namespace Job (db) is unchanged
    dbspec = next(s for s in runner.started if s.kind == "db")
    _, dbjob = job_manifests(dbspec)
    assert dbjob["metadata"]["namespace"] == config.BACKUP_NAMESPACE
    assert dbjob["spec"]["template"]["spec"]["serviceAccountName"] == "backup-job"
    assert "affinity" not in dbjob["spec"]["template"]["spec"]


def test_volume_job_has_no_affinity_when_the_app_is_scaled_to_zero(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    db_session.get(models.App, aid).status = "suspended"
    db_session.commit()
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    spec = runner.started[0]
    assert spec.volume["app_pod_label"] is None
    _, job = job_manifests(spec)
    assert "affinity" not in job["spec"]["template"]["spec"]
    # the volume is still mounted read-only
    assert any(v.get("persistentVolumeClaim", {}).get("readOnly") for v in job["spec"]["template"]["spec"]["volumes"])


def test_volume_job_uses_the_apps_uid_and_clamps_to_its_limits(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    app = db_session.get(models.App, aid)
    app.run_as_uid, app.cpu_limit_m, app.mem_limit_mi = 33, 80, 200
    db_session.commit()
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    _, job = job_manifests(runner.started[0])
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 33 and pod["securityContext"]["fsGroup"] == config.VOLUME_FS_GROUP
    lim = pod["containers"][0]["resources"]["limits"]
    assert lim["cpu"] == "80m" and lim["memory"] == "200Mi"  # never above the LimitRange max the app itself is held to
    assert pod["containers"][0]["env"] == [{"name": "GOMEMLIMIT", "value": "160MiB"}]


def test_volume_run_fails_clearly_when_the_claim_is_not_bound(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False, phase="Pending")
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    run = db_session.query(models.BackupRun).one()
    assert run.status == "failed" and run.error == "the volume isn't ready yet" and runner.started == []
    assert db_session.query(models.AppEvent).filter_by(kind="backup_failed").count() == 1


def test_volume_success_queues_a_prune_in_the_platform_namespace(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    vspec = runner.started[0]
    runner.finish(vspec.name, {"snapshot_id": "ab12cd34", "bytes_added": 700})  # a volume Job reports no repo_bytes
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    vrun = db_session.query(models.BackupRun).filter_by(trigger="manual").one()
    assert (vrun.status, vrun.snapshot_id, vrun.bytes_added, vrun.repo_bytes) == ("succeeded", "ab12cd34", 700, None)
    assert vspec.name in runner.cleaned
    # tick already started the queued prune: platform namespace, no volume mount, retention policy in the env
    prune = next(s for s in runner.started if s.env["MODE"] == "prune")
    assert prune.namespace is None and prune.volume is None and prune.kind == "volume" and prune.scratch_mib == 0
    assert prune.env["KEEP_DAILY"] == "1" and prune.env["KEEP_WEEKLY"] == "1" and prune.env["KEEP_MONTHLY"] == "1"
    assert prune.env["RESTIC_REPOSITORY"].endswith(f"/apps/{aid}/volume")
    _, job = job_manifests(prune)
    assert job["metadata"]["namespace"] == config.BACKUP_NAMESPACE
    runner.finish(prune.name, {"pruned": True, "repo_bytes": 4321})
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.vol_repo_bytes == 4321 and cfg.vol_last_status == "succeeded"
    assert [a.action for a in db_session.query(models.AuditLog).filter(models.AuditLog.action.in_(["backup.pruned", "backup.succeeded"]))
            ] == ["backup.succeeded", "backup.pruned"]
    # the prune is not a backup: not listed, and the owner can back up again after the cooldown
    body = user.get(f"/apps/{aid}/backups").json()
    assert [r["trigger"] for r in body["runs"]] == ["manual"] and len(body["backups"]) == 1


def test_a_failed_prune_is_audited_but_is_not_a_backup_failure(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    runner.finish(runner.started[0].name, {"snapshot_id": "ab12cd34", "bytes_added": 1})
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    prune = next(s for s in runner.started if s.env["MODE"] == "prune")
    runner.fail(prune.name, "retention cleanup failed")
    backups.tick(db_session, runner, NOW + timedelta(minutes=2))
    assert db_session.query(models.AppEvent).count() == 0
    assert rows(db_session, "backup.prune_failed")
    cfg = db_session.get(models.AppBackupConfig, aid)
    assert cfg.vol_last_status == "succeeded" and cfg.last_status == "succeeded"


def test_leftover_volume_secrets_are_swept_in_the_app_namespace(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    user.post(f"/apps/{aid}/backups")
    backups.tick(db_session, runner, NOW)
    backups.tick(db_session, runner, NOW + timedelta(minutes=1))
    assert runner.swept_namespaces and runner.swept_namespaces[-1] == [f"app-{aid}"]


def test_volume_export_and_delete_per_run(app_ctx, user, db_session, runner):
    aid = app_ctx
    sid, ids = _mk_set(db_session, aid, {"volume": "succeeded"})
    r = user.post(f"/apps/{aid}/backups/{ids['volume']}/download")
    assert r.status_code == 202 and r.json()["kind"] == "volume"
    backups.tick(db_session, runner, datetime.utcnow())
    spec = runner.started[-1]
    assert spec.env["MODE"] == "export" and spec.env["KIND"] == "volume" and spec.env["EXPORT_KEY"].endswith(".tar.gz")
    assert spec.env["VOLUME_DIR"] == config.BACKUP_VOLUME_MOUNT and spec.scratch_mib >= 1024
    runner.finish(spec.name, {"exported": True, "bytes": 1})
    backups.tick(db_session, runner, datetime.utcnow())
    assert user.delete(f"/apps/{aid}/backups/{ids['volume']}").status_code == 202


def test_purge_of_a_volume_repo_runs_in_the_platform_namespace(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    cfg = backups.ensure_config(db_session, db_session.get(models.App, aid), NOW)
    cfg.enabled, cfg.vol_repo_bytes, cfg.deleted_retention_until = False, 100, NOW - timedelta(days=1)
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    spec = next(s for s in runner.started if s.env["MODE"] == "purge")
    assert spec.kind == "volume" and spec.namespace is None


# --- manifests: egress policy and quota headroom -----------------------------------------------

def test_network_policies_unchanged_without_volume_backups():
    pols = k8s.network_policies(1, 8080, None)
    assert {p["metadata"]["name"] for p in pols} == {"default-deny", "allow-ingress-controller", "allow-egress-dns",
                                                     "allow-egress-internet", "allow-egress-mysql"}
    assert all(p["spec"]["podSelector"] == {} for p in pols)


def test_backup_pod_is_excluded_from_the_apps_allow_rules_and_gets_only_the_bucket():
    pols = {p["metadata"]["name"]: p for p in k8s.network_policies(7, 8080, None, storage_ips=["10.1.1.1"], backup_ips=["10.2.2.2", "10.2.2.2"])}
    not_backup = {"matchExpressions": [{"key": "picopaas.io/backup", "operator": "DoesNotExist"}]}
    for name in ("allow-ingress-controller", "allow-egress-internet", "allow-egress-mysql", "allow-egress-storage"):
        assert pols[name]["spec"]["podSelector"] == not_backup, name
    assert pols["default-deny"]["spec"]["podSelector"] == {} and pols["allow-egress-dns"]["spec"]["podSelector"] == {}
    bk = pols["allow-egress-backup"]
    assert bk["metadata"]["namespace"] == "app-7" and bk["spec"]["podSelector"] == {"matchLabels": {"picopaas.io/backup": "1"}}
    assert bk["spec"]["policyTypes"] == ["Egress"]
    (rule,) = bk["spec"]["egress"]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]
    assert rule["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0" and "10.0.0.0/8" in rule["to"][0]["ipBlock"]["except"]
    assert rule["to"][1:] == [{"ipBlock": {"cidr": "10.2.2.2/32"}}]
    assert "database" not in json.dumps(bk).lower()


def test_same_namespace_policy_skips_the_backup_pod():
    pols = {p["metadata"]["name"]: p for p in k8s.network_policies(7, 8080, None, replicas=2, backup_ips=[])}
    assert pols[k8s.SAME_NAMESPACE_POLICY]["spec"]["podSelector"]["matchExpressions"][0]["key"] == "picopaas.io/backup"
    assert pols["allow-egress-backup"]["spec"]["egress"][0]["to"][1:] == []


def test_quota_gets_exactly_one_backup_pod_of_headroom():
    base = k8s.resource_quota_manifest(7, None, volume=VOL)["spec"]["hard"]
    pod = k8s.backup_pod_resources(None)
    withbk = k8s.resource_quota_manifest(7, None, volume=VOL, backup_pod=pod)["spec"]["hard"]
    assert pod["cpu_limit_m"] == min(200, config.DEFAULT_CPU_LIMIT_M)
    assert int(withbk["pods"]) == int(base["pods"]) + 1
    assert int(withbk["limits.cpu"][:-1]) == int(base["limits.cpu"][:-1]) + pod["cpu_limit_m"]
    assert int(withbk["limits.memory"][:-2]) == int(base["limits.memory"][:-2]) + pod["mem_limit_mi"]
    assert int(withbk["limits.ephemeral-storage"][:-2]) == int(base["limits.ephemeral-storage"][:-2]) + pod["eph_limit_mi"]
    assert withbk["persistentvolumeclaims"] == base["persistentvolumeclaims"] == "1"


def _sweep_apply(db, kube):
    reconcile_all(db, kube=kube, mysql_admin_factory=lambda s: FakeMySQLAdmin(), object_store=FakeObjectStore(), settings={})
    db.expire_all()


def _applied(kube, aid, kind):
    return [m for m in kube.applied.get(f"app-{aid}", []) if m["kind"] == kind]


def test_reconciler_renders_headroom_and_policy_only_with_the_gate_and_a_volume(on, vols, scoped, user, db_session):
    with_vol = _volume_app(user, db_session, "has-volume", with_db=False)
    no_vol = user.post("/apps", json={"name": "plain", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}).json()["id"]
    kube = FakeKubeProvider()
    _sweep_apply(db_session, kube)
    names = {m["metadata"]["name"] for m in _applied(kube, with_vol, "NetworkPolicy")}
    assert "allow-egress-backup" in names
    assert "allow-egress-backup" not in {m["metadata"]["name"] for m in _applied(kube, no_vol, "NetworkPolicy")}
    q_vol = _applied(kube, with_vol, "ResourceQuota")[-1]["spec"]["hard"]
    q_plain = _applied(kube, no_vol, "ResourceQuota")[-1]["spec"]["hard"]
    assert int(q_vol["pods"]) == int(q_plain["pods"]) + 1


def test_reconciler_without_the_gate_renders_nothing_extra(on, vols, user, db_session):
    aid = _volume_app(user, db_session, "has-volume", with_db=False)
    kube = FakeKubeProvider()
    _sweep_apply(db_session, kube)
    assert "allow-egress-backup" not in {m["metadata"]["name"] for m in _applied(kube, aid, "NetworkPolicy")}
    assert int(_applied(kube, aid, "ResourceQuota")[-1]["spec"]["hard"]["pods"]) == 2


# --- RBAC -------------------------------------------------------------------------------------

def test_platform_sa_can_run_jobs_only_inside_app_namespaces():
    from pathlib import Path
    docs = [d for d in yaml.safe_load_all((Path(__file__).resolve().parent.parent / "deploy/platform/rbac.yaml").read_text()) if d]
    roles = {d["metadata"]["name"]: d["rules"] for d in docs if d["kind"] == "ClusterRole"}
    jobs = [r for r in roles["picopaas-app-ns"] if "jobs" in r.get("resources", [])]
    assert len(jobs) == 1 and jobs[0]["apiGroups"] == ["batch"] and set(jobs[0]["verbs"]) == {"get", "list", "create", "delete"}
    assert not any("jobs" in r.get("resources", []) for r in roles["picopaas-reconciler"])  # never cluster-wide
    secrets = [r for r in roles["picopaas-app-ns"] if "secrets" in r.get("resources", [])]
    assert secrets and {"create", "delete", "list"} <= set(secrets[0]["verbs"])


def test_package_scratch_matches_the_parts(app_ctx, user, db_session):
    aid = app_ctx
    sid, _ = _mk_set(db_session, aid, {"db": "succeeded"})
    user.post(f"/apps/{aid}/backups/sets/{sid}/download")
    run = db_session.query(models.BackupRun).filter_by(trigger="export").one()
    assert backups._scratch_mib(db_session, run) == 64 + 256


# --- the real runner uses the Job's namespace -------------------------------------------------

def test_kube_runner_works_in_the_app_namespace():
    from types import SimpleNamespace
    from app.providers.backup import KubeBackupRunner
    calls = []

    class Res:
        def __init__(self, kind):
            self.kind = kind

        def create(self, body, namespace):
            calls.append(("create", self.kind, namespace))

        def get(self, name=None, namespace=None, label_selector=None):
            calls.append(("get", self.kind, namespace))
            if name is None:
                return {"items": [{"metadata": {"name": "bk-1"}}, {"metadata": {"name": "bk-9"}}]}
            return {"metadata": {"name": name}, "status": {"succeeded": 1}}

        def delete(self, name, namespace, body=None):
            calls.append(("delete", self.kind, namespace, name))

    class Core:
        def list_namespaced_pod(self, ns, label_selector):
            calls.append(("pods", ns))
            return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="p", creation_timestamp=1))])

        def read_namespaced_pod_log(self, name, ns, tail_lines):
            calls.append(("log", ns))
            return 'PICOPAAS_RESULT {"snapshot_id": "ab12cd34"}'

    dyn = SimpleNamespace(resources=SimpleNamespace(get=lambda api_version, kind: Res(kind)))
    r = KubeBackupRunner(dyn, Core())
    spec = BackupJobSpec(name="bk-1", run_id=1, app_id=4, kind="volume", env={"RESTIC_PASSWORD": "not-the-password"},
                         namespace="app-4", volume={"claim": "app-4-data", "uid": 10001, "fs_group": 10001, "app_pod_label": "app-4",
                                                    **k8s.backup_pod_resources(None)})
    r.start(spec)
    assert ("create", "Secret", "app-4") in calls and ("create", "Job", "app-4") in calls
    st = r.status("bk-1", "app-4")
    assert st.state == "succeeded" and st.result == {"snapshot_id": "ab12cd34"}
    assert ("pods", "app-4") in calls and ("log", "app-4") in calls
    r.cleanup("bk-1", "app-4")
    assert ("delete", "Job", "app-4", "bk-1") in calls and ("delete", "Secret", "app-4", "bk-1") in calls
    calls.clear()
    assert r.sweep_namespaces(["app-4", "app-5"], {"bk-1"}) == 2  # bk-9 is not active in either namespace
    assert ("delete", "Secret", "app-4", "bk-9") in calls and ("delete", "Secret", "app-5", "bk-9") in calls
    assert not any(c[0] == "delete" and c[3] == "bk-1" for c in calls)


def test_prune_after_the_final_backup_of_a_deleted_app_still_runs(on, vols, scoped, user, db_session, runner):
    aid = _volume_app(user, db_session, with_db=False)
    backups.ensure_config(db_session, db_session.get(models.App, aid), NOW).repo_password_enc = None
    db_session.get(models.App, aid).deleted_at = NOW
    db_session.add(models.BackupRun(app_id=aid, kind="volume", trigger="prune", status="queued", created_at=NOW))
    db_session.commit()
    backups.tick(db_session, runner, NOW)
    assert [s.env["MODE"] for s in runner.started] == ["prune"]
