"""Coverage pass (2026-10-24): platform settings, scheduler registration, health namespace discovery, the real
KubeBackupRunner / KubeClientProvider methods against a fake dynamic client and stubbed boto3, sweeper gaps."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import backup_sweeper, backups, config, health, models, platform_settings as ps, scheduler
from app.providers.backup import BackupRunnerError, KubeBackupRunner, _parse_ts
from app.providers.kube import KubeClientProvider
from tests.conftest import register_and_login
from tests.test_backup_sweeper import LATE, running_run, sweep
from tests.test_backups import NOW, admin, on, other, runner, user  # noqa: F401
from tests.test_backups_limits import aid, api, mkset, rows  # noqa: F401

kexc = pytest.importorskip("kubernetes.dynamic.exceptions")
_NF = SimpleNamespace(status=404, reason="nf", data="", headers={}, body="")


# --- platform_settings ----------------------------------------------------------------------------------

def test_settings_cache_ttl_and_invalidate(db_session, monkeypatch):
    ps.invalidate()
    t = [1000.0]
    monkeypatch.setattr(ps.time, "monotonic", lambda: t[0])
    assert ps.get(db_session, "max_replicas") == ps.default("max_replicas")
    db_session.add(models.PlatformSetting(key="max_replicas", value="3"))
    db_session.commit()
    assert ps.get(db_session, "max_replicas") == ps.default("max_replicas")  # cached
    t[0] += ps.CACHE_TTL_SECONDS - 0.1
    assert ps.get(db_session, "max_replicas") == ps.default("max_replicas")  # still inside the TTL
    t[0] += 0.2
    assert ps.get(db_session, "max_replicas") == 3  # expired: re-read
    db_session.query(models.PlatformSetting).delete()
    db_session.commit()
    assert ps.get(db_session, "max_replicas") == 3
    ps.invalidate()
    assert ps.get(db_session, "max_replicas") == ps.default("max_replicas")


def test_settings_unreadable_table_behaves_as_defaults(db_session, monkeypatch):
    ps.invalidate()
    monkeypatch.setattr(db_session, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table")))
    assert ps.get(db_session, "max_replicas") == ps.default("max_replicas")
    assert ps.get(None, "max_replicas") == ps.default("max_replicas")
    ps.invalidate()


def test_settings_corrupt_value_falls_back_to_default_and_unknown_key(db_session):
    ps.invalidate()
    db_session.add(models.PlatformSetting(key="max_replicas", value="not-a-number"))
    db_session.commit()
    assert ps.get(db_session, "max_replicas") == ps.default("max_replicas")
    with pytest.raises(KeyError):
        ps.get(db_session, "no_such_key")
    ps.invalidate()


def test_settings_bool_roundtrip_and_validation():
    b = ps.BY_KEY["backups_enabled_default"]
    assert ps.parse(b, "1") is True and ps.parse(b, "0") is False
    assert ps.serialize(b, True) == "1" and ps.serialize(b, False) == "0"
    assert ps.validate_value(b, True) is None and ps.validate_value(b, False) is None
    assert ps.validate_value(b, 1) == "must be true or false"
    assert ps.validate_value(b, "true") == "must be true or false"
    i = ps.BY_KEY["max_replicas"]
    assert ps.validate_value(i, True) == "must be a whole number"  # bool is not an int here
    assert ps.validate_value(i, 2.0) == "must be a whole number"
    assert ps.validate_value(i, "3") == "must be a whole number"
    assert ps.validate_value(i, i.min - 1).startswith("must be at least")
    assert ps.validate_value(i, i.max + 1).startswith("must be at most")
    assert ps.validate_value(i, i.min) is None and ps.validate_value(i, i.max) is None
    assert ps.serialize(i, 7) == "7" and ps.parse(i, "7") == 7


def test_settings_update_atomic_unknown_invariants_reset_and_effective_volume(db_session):
    ps.invalidate()
    errs = ps.update(db_session, {"max_replicas": 4, "bogus": 1}, "a@example.com")
    assert errs == ["bogus: unknown setting"] and ps.get(db_session, "max_replicas") == ps.default("max_replicas")
    # invariants, each one on its own
    assert any("volume_max_gi" in e for e in ps.update(db_session, {"volume_max_gi": 50, "override_volume_max_gi": 10}, None))
    assert any("default_app_limit" in e for e in ps.update(db_session, {"default_app_limit": 9, "max_apps_per_user": 5}, None))
    assert any("backup_max_bytes_per_app" in e for e in ps.update(
        db_session, {"backup_max_bytes_per_app": 3 * 1024**3, "backup_max_bytes_total": 2 * 1024**3}, None))
    assert db_session.query(models.PlatformSetting).count() == 0  # nothing saved on any failure
    # invariant is judged against the *effective* other value, not just the payload
    assert ps.update(db_session, {"override_volume_max_gi": 20, "volume_max_gi": 15}, "a@example.com") == []
    assert ps.effective_volume_max_gi(db_session) == 15
    assert any("volume_max_gi" in e for e in ps.update(db_session, {"override_volume_max_gi": 5}, None))
    assert ps.update(db_session, {"override_volume_max_gi": 40}, "a@example.com") == []
    assert ps.effective_volume_max_gi(db_session) == 15
    assert ps.update(db_session, {"volume_max_gi": 99}, None)  # 99 > 40
    # update an existing row, then reset one that exists and one that doesn't
    assert ps.update(db_session, {"override_volume_max_gi": 41}, "b@example.com") == []
    assert db_session.get(models.PlatformSetting, "override_volume_max_gi").updated_by == "b@example.com"
    assert ps.update(db_session, {"override_volume_max_gi": None}, None)  # reset would drop below volume_max_gi=15: refused
    assert ps.update(db_session, {"volume_max_gi": None, "override_volume_max_gi": None, "max_replicas": None}, None) == []
    assert ps.get(db_session, "override_volume_max_gi") == ps.default("override_volume_max_gi")
    ps.invalidate()


def test_settings_describe_all_marks_overridden(db_session):
    ps.invalidate()
    ps.update(db_session, {"max_replicas": 2}, "a@example.com")
    flat = {s["key"]: s for g in ps.describe_all(db_session) for s in g["settings"]}
    assert flat["max_replicas"]["overridden"] and flat["max_replicas"]["updated_by"] == "a@example.com"
    assert flat["max_replicas"]["updated_at"] and flat["max_replicas"]["value"] == 2
    assert not flat["cpu_limit_max_m"]["overridden"] and flat["cpu_limit_max_m"]["updated_at"] is None
    assert [g["name"] for g in ps.describe_all(db_session)] == [ps.G_CEIL, ps.G_APPDEF, ps.G_DEF, ps.G_BAK]
    ps.invalidate()


def test_settings_router_validation_audit_and_reset(make_client, db_session):
    ps.invalidate()
    admin_c = make_client()
    register_and_login(admin_c, "admin@example.com")
    member = make_client()
    register_and_login(member, "member@example.com")
    assert member.get("/admin/settings").status_code == 403
    assert member.put("/admin/settings", json={"settings": {"max_replicas": 2}}).status_code == 403
    assert admin_c.put("/admin/settings", json={"settings": {}}).status_code == 422
    assert admin_c.put("/admin/settings", json={"settings": {"max_replicas": 2}, "x": 1}).status_code == 422  # extra=forbid
    r = admin_c.put("/admin/settings", json={"settings": {"max_replicas": True}})
    assert r.status_code == 422 and "whole number" in r.json()["detail"]
    r = admin_c.put("/admin/settings", json={"settings": {"backups_enabled_default": 1}})
    assert r.status_code == 422 and "true or false" in r.json()["detail"]
    r = admin_c.put("/admin/settings", json={"settings": {"nope": 1}})
    assert r.status_code == 422 and "unknown" in r.json()["detail"]
    r = admin_c.put("/admin/settings", json={"settings": {"max_replicas": 2}})
    assert r.status_code == 200
    r = admin_c.put("/admin/settings", json={"settings": {"max_replicas": None}})
    assert r.status_code == 200
    msgs = [a.detail for a in rows(db_session, "settings.update")]
    assert msgs[-1].endswith("(reset to default)") and "-> 2" in msgs[0]
    ps.invalidate()


# --- scheduler: backup jobs ----------------------------------------------------------------------------

def test_backup_jobs_skip_when_unusable_or_not_leader(monkeypatch, sess_fixture=None):
    called = []
    monkeypatch.setattr(backups, "usable", lambda: False)
    monkeypatch.setattr(backups, "tick", lambda *a: called.append("tick"))
    monkeypatch.setattr(backup_sweeper, "sweep", lambda *a: called.append("sweep"))
    scheduler.backup_job()
    scheduler.backup_sweep_job()
    monkeypatch.setattr(backups, "usable", lambda: True)
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: False)
    scheduler.backup_job()
    scheduler.backup_sweep_job()
    assert called == []


@pytest.mark.parametrize("job,target,name", [("backup_job", backups, "tick"), ("backup_sweep_job", backup_sweeper, "sweep")])
def test_backup_jobs_run_and_isolate_failures(db_session, monkeypatch, caplog, job, target, name):
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    closed = []
    monkeypatch.setattr(db_session, "close", lambda: closed.append(1))
    monkeypatch.setattr(backups, "usable", lambda: True)
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    seen = []
    monkeypatch.setattr(target, name, lambda s, r, *a: seen.append(type(r).__name__))
    getattr(scheduler, job)()
    assert seen == ["KubeBackupRunner"] and closed == [1]
    monkeypatch.setattr(target, name, lambda s, r, *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.ERROR):
        getattr(scheduler, job)()  # must not raise
    assert closed == [1, 1] and "failed" in caplog.text


def test_backup_sweep_registered_only_when_backups_enabled(monkeypatch):
    for enabled in (True, False):
        jobs = {}

        class FakeSched:
            def add_job(self, fn, trigger, id=None, **kw):
                jobs[id] = (fn, trigger, kw)

            def start(self):
                pass

        monkeypatch.setattr(scheduler, "BackgroundScheduler", FakeSched)
        monkeypatch.setattr(health, "start_background", lambda getter: None)
        monkeypatch.setattr(config, "BACKUPS_ENABLED", enabled)
        scheduler.start_scheduler()
        if enabled:
            fn, trigger, kw = jobs["backup_sweep"]
            assert fn is scheduler.backup_sweep_job and trigger == "interval"
            assert kw["minutes"] == config.BACKUP_SWEEP_MINUTES and kw["max_instances"] == 1 and kw["coalesce"] is True
            assert kw["next_run_time"] is not None and jobs["backup"][0] is scheduler.backup_job
        else:
            assert "backup_sweep" not in jobs and "backup" not in jobs


# --- health._platform_namespaces ------------------------------------------------------------------------

def test_platform_namespaces_known_first_then_discovered_sorted_never_tenant():
    by_ns = {"app-1": [], "zeta-system": [], "picopaas-backup": [], "kube-system": [], "default": [], "traefik": []}
    out = health._platform_namespaces(by_ns)
    assert out[:len(health.PLATFORM_NAMESPACES)] == list(health.PLATFORM_NAMESPACES)
    assert "app-1" not in out and "default" not in out
    assert out[len(health.PLATFORM_NAMESPACES):] == sorted(out[len(health.PLATFORM_NAMESPACES):])
    assert "picopaas-backup" in out and "kube-system" not in out
    assert health._platform_namespaces({}) == list(health.PLATFORM_NAMESPACES)


# --- KubeBackupRunner against a fake dynamic client ----------------------------------------------------

class _Dyn:
    """items: {(ns, kind): {name: obj}}; get/delete honour the namespace, failures by (ns, kind) -> exception."""

    def __init__(self):
        self.items, self.fail, self.deleted = {}, {}, []
        self.resources = SimpleNamespace(get=lambda api_version, kind: _R(self, kind))


class _R:
    def __init__(self, d, kind):
        self.d, self.kind = d, kind

    def get(self, name=None, namespace=None, label_selector=None):
        exc = self.d.fail.get((namespace, self.kind)) or self.d.fail.get((None, self.kind))
        if exc:
            raise exc
        if name is None and namespace is None:  # cluster-wide list
            return {"items": [o for (n, k), objs in self.d.items.items() if k == self.kind for o in objs.values()]}
        return {"items": list(self.d.items.get((namespace, self.kind), {}).values())}

    def delete(self, name, namespace, body=None):
        objs = self.d.items.get((namespace, self.kind), {})
        if name not in objs:
            raise kexc.NotFoundError(_NF)
        del objs[name]
        self.d.deleted.append((namespace, self.kind, name))


def _obj(name, age_s, labels=None):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"metadata": {"name": name, "creationTimestamp": ts, "labels": labels or {}}}


def _put(d, ns, kind, *objs):
    d.items.setdefault((ns, kind), {}).update({o["metadata"]["name"]: o for o in objs})


def _runner(d=None):
    d = d or _Dyn()
    return KubeBackupRunner(d, SimpleNamespace()), d


class ForbiddenLike(Exception):
    status = 403


def test_sweep_orphans_deletes_old_inactive_only_and_pod_follows_its_job(monkeypatch):
    r, d = _runner()
    ns = config.BACKUP_NAMESPACE
    _put(d, ns, "Job", _obj("bk-1", 9999), _obj("bk-2", 9999), _obj("bk-3", 5))
    _put(d, ns, "Secret", _obj("bk-1", 9999), _obj("bk-9", 9999))
    _put(d, ns, "Pod", _obj("bk-1-xyz", 9999, {"job-name": "bk-1"}), _obj("bk-7-abc", 9999, {"job-name": "bk-7"}), _obj("noage", 0))
    d.items[(ns, "Pod")]["noage"]["metadata"]["creationTimestamp"] = "garbage"  # unparseable age: never deleted
    out = r.sweep_orphans([ns], {"bk-1"}, 600, 50)
    assert out == {"jobs": 1, "pods": 1, "secrets": 1}
    assert set(d.items[(ns, "Job")]) == {"bk-1", "bk-3"}  # bk-2 gone, active bk-1 and young bk-3 kept
    assert set(d.items[(ns, "Secret")]) == {"bk-1"}
    assert set(d.items[(ns, "Pod")]) == {"bk-1-xyz", "noage"}
    assert r.sweep_orphans([ns], {"bk-1"}, 600, 50) == {"jobs": 0, "pods": 0, "secrets": 0}  # idempotent


def test_sweep_orphans_limit_bound_and_namespace_errors():
    r, d = _runner()
    ns, tenant = config.BACKUP_NAMESPACE, "app-5"
    _put(d, ns, "Job", *[_obj(f"bk-{i}", 9999) for i in range(10)])
    _put(d, tenant, "Secret", _obj("bk-50", 9999))
    assert sum(r.sweep_orphans([ns, tenant], set(), 60, 3).values()) == 3
    assert r.sweep_orphans([ns, tenant], set(), 60, 0) == {"jobs": 0, "pods": 0, "secrets": 0}
    # gone / forbidden namespaces are skipped quietly; other errors are logged and the rest still runs
    d.fail[(ns, "Job")] = kexc.NotFoundError(_NF)
    d.fail[(ns, "Secret")] = ForbiddenLike()
    d.fail[(ns, "Pod")] = RuntimeError("api blip")
    out = r.sweep_orphans([ns, tenant], set(), 60, 50)
    assert out["secrets"] == 1 and out["jobs"] == 0  # tenant secret swept, platform ns unreadable


def test_sweep_orphans_stops_inside_a_listing_when_limit_hits():
    r, d = _runner()
    _put(d, "a", "Job", *[_obj(f"bk-{i}", 9999) for i in range(5)])
    assert r.sweep_orphans(["a"], set(), 60, 2)["jobs"] == 2
    assert len(d.items[("a", "Job")]) == 3


def test_sweep_namespaces_skips_gone_and_forbidden_and_logs_others(caplog):
    r, d = _runner()
    _put(d, "app-1", "Secret", _obj("bk-1", 1))
    d.fail[("app-2", "Secret")] = ForbiddenLike()
    d.fail[("app-3", "Secret")] = kexc.NotFoundError(_NF)
    d.fail[("app-4", "Secret")] = RuntimeError("blip")
    with caplog.at_level(logging.WARNING):
        assert r.sweep_namespaces(["app-1", "app-2", "app-3", "app-4"], set()) == 1
    assert "app-4" in caplog.text and "app-2" not in caplog.text


def test_parse_ts_variants():
    assert _parse_ts("2026-10-24T10:00:00Z") == datetime(2026, 10, 24, 10, tzinfo=timezone.utc)
    assert _parse_ts(datetime(2026, 1, 1)).tzinfo is not None
    assert _parse_ts(datetime(2026, 1, 1, tzinfo=timezone.utc)).tzinfo is not None
    assert _parse_ts("nope") is None and _parse_ts(None) is None


class _S3:
    def __init__(self, uploads=None, prefixes=None, fail=False):
        self.uploads, self.prefixes, self.fail, self.aborted = uploads or {}, prefixes or [], fail, []

    def get_paginator(self, name):
        if self.fail:
            raise RuntimeError("endpoint leak")
        if name == "list_multipart_uploads":
            return SimpleNamespace(paginate=lambda Bucket, Prefix: iter([{"Uploads": self.uploads.get(Prefix, [])}, {}]))
        return SimpleNamespace(paginate=lambda Bucket, Prefix, Delimiter: iter(
            [{"CommonPrefixes": [{"Prefix": f"{Prefix}{p}/"} for p in self.prefixes]}, {}]))

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.aborted.append((Key, UploadId))


def _up(key, age_s):
    return {"Key": key, "UploadId": "u-" + key, "Initiated": datetime.now(timezone.utc) - timedelta(seconds=age_s)}


def test_abort_stale_uploads_age_limit_prefixes_and_errors(monkeypatch):
    r, _ = _runner()
    s3 = _S3({config.BACKUP_EXPORT_PREFIX: [_up("e/old", 9999), _up("e/new", 5)],
              config.BACKUP_PREFIX: [_up("r/old1", 9999), _up("r/old2", 9999)]})
    monkeypatch.setattr(r, "_s3", lambda: s3)
    assert r.abort_stale_uploads(3600, 100) == 3
    assert sorted(k for k, _ in s3.aborted) == ["e/old", "r/old1", "r/old2"]  # the young upload is untouched
    s3.aborted.clear()
    assert r.abort_stale_uploads(3600, 2) == 2  # bounded
    monkeypatch.setattr(r, "_s3", lambda: _S3(fail=True))
    with pytest.raises(BackupRunnerError, match="RuntimeError") as e:
        r.abort_stale_uploads(1, 1)
    assert "leak" not in str(e.value)
    monkeypatch.setattr(config, "BACKUP_EXPORT_PREFIX", config.BACKUP_PREFIX)
    with pytest.raises(BackupRunnerError, match="unsafe"):
        r.abort_stale_uploads(1, 1)


def test_repo_app_ids_only_numeric_prefixes(monkeypatch):
    r, _ = _runner()
    monkeypatch.setattr(r, "_s3", lambda: _S3(prefixes=["3", "17", "x9", "", "0"]))
    assert r.repo_app_ids() == {3, 17, 0}
    monkeypatch.setattr(r, "_s3", lambda: _S3(fail=True))
    with pytest.raises(BackupRunnerError, match="RuntimeError") as e:
        r.repo_app_ids()
    assert "leak" not in str(e.value)


def test_delete_exports_guards_and_errors(monkeypatch):
    r, _ = _runner()
    calls = []
    s3 = SimpleNamespace(delete_objects=lambda **kw: calls.append(kw))
    monkeypatch.setattr(r, "_s3", lambda: s3)
    p = config.BACKUP_EXPORT_PREFIX
    r.delete_exports([])
    assert calls == []
    r.delete_exports([p + "1/a.gz"])
    assert calls[0]["Delete"]["Objects"] == [{"Key": p + "1/a.gz"}]
    for bad in (["repo/x"], [p + "../repos/x"]):
        with pytest.raises(BackupRunnerError, match="unsafe"):
            r.delete_exports(bad)
    monkeypatch.setattr(r, "_s3", lambda: SimpleNamespace(delete_objects=lambda **kw: (_ for _ in ()).throw(RuntimeError("x"))))
    with pytest.raises(BackupRunnerError, match="RuntimeError"):
        r.delete_exports([p + "1/a.gz"])


# --- KubeClientProvider.platform_pods / platform_log ---------------------------------------------------

def _pod(ns, name, containers=("app",), phase="Running", ready=True, restarts=0, cs=True):
    st = {"phase": phase}
    if cs:
        st["containerStatuses"] = [{"ready": ready, "restartCount": restarts}]
    return {"metadata": {"namespace": ns, "name": name}, "status": st,
            "spec": {"containers": [{"name": c} for c in (*containers, None)]}}


class _Core:
    def __init__(self, text="a\nb\n"):
        self.text, self.calls = text, []

    def read_namespaced_pod_log(self, **kw):
        self.calls.append(kw)
        return self.text


def _kube(pods, core=None):
    d = _Dyn()
    d.items[("*", "Pod")] = {f"{i}": p for i, p in enumerate(pods)}
    return KubeClientProvider(d, core or _Core())


def test_platform_pods_filters_tenants_sorts_and_summarises():
    k = _kube([_pod("app-1", "web"), _pod("traefik", "t-0"), _pod("picopaas-system", "z", restarts=2, ready=False),
               _pod("picopaas-system", "a", cs=False, phase="Pending"),
               {"metadata": {"namespace": "picopaas-system"}, "status": {}}])  # no name: skipped
    # the fake returns every Pod across namespaces for a cluster-wide get()
    out = k.platform_pods()
    assert [(p["namespace"], p["name"]) for p in out] == [("picopaas-system", "a"), ("picopaas-system", "z"), ("traefik", "t-0")]
    a, z = out[0], out[1]
    assert a["ready"] is False and a["restarts"] == 0 and a["phase"] == "Pending" and a["containers"] == ["app"]
    assert z["ready"] is False and z["restarts"] == 2
    assert out[2]["ready"] is True


def test_platform_log_validates_namespace_and_pod_and_passes_options():
    core = _Core("x\n" + "y" * 5000)
    k = _kube([_pod("traefik", "t-0", containers=("traefik",))], core)
    with pytest.raises(ValueError):
        k.platform_log("app-1", "web", "app", 10)
    with pytest.raises(LookupError):
        k.platform_log("traefik", "t-0", "other-container", 10)  # only listed names reach the API
    with pytest.raises(LookupError):
        k.platform_log("traefik", "nope", "traefik", 10)
    lines = k.platform_log("traefik", "t-0", "traefik", 25, since_seconds=60, previous=True)
    assert lines[0] == "x" and len(lines[1]) < 5000
    kw = core.calls[-1]
    assert kw["tail_lines"] == 25 and kw["previous"] is True and kw["since_seconds"] == 60 and kw["namespace"] == "traefik"
    k.platform_log("traefik", "t-0", "traefik", 5)
    assert "since_seconds" not in core.calls[-1]
    core.text = None
    assert k.platform_log("traefik", "t-0", "traefik", 5) == []


# --- backup_sweeper gaps -------------------------------------------------------------------------------

def test_sweeper_status_check_failure_is_reported_and_run_left_alone(db_session, aid, runner, monkeypatch):
    rid = running_run(db_session, aid, runner=runner)
    monkeypatch.setattr(runner, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
    res = sweep(db_session, runner)
    db_session.expire_all()
    assert db_session.get(models.BackupRun, rid).status == "running"
    assert any("status check failed" in e for e in res["errors"])


def test_sweeper_run_without_job_name_fails_as_interrupted(db_session, aid, runner):
    rid = running_run(db_session, aid, runner=None, with_job=False)
    db_session.query(models.BackupRun).filter_by(id=rid).update({"job_name": None})
    db_session.commit()
    res = sweep(db_session, runner)
    db_session.expire_all()
    assert db_session.get(models.BackupRun, rid).error == backups.INTERRUPTED_MSG and res["cleaned"]["runs_failed"] == 1


def test_sweeper_k8s_phase_failure_is_isolated_and_blob_errors_named(db_session, aid, runner, monkeypatch):
    mkset(db_session, aid, 5)
    monkeypatch.setattr(runner, "sweep_orphans", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    for name in ("sweep_exports", "abort_stale_uploads", "repo_app_ids"):
        monkeypatch.setattr(runner, name, lambda *a, **k: (_ for _ in ()).throw(BackupRunnerError("x")))
    res = sweep(db_session, runner)
    assert len(res["errors"]) == backup_sweeper.MAX_ERRORS  # capped; the rows phase still ran (no exception)
    assert res["errors"][0].startswith("kubernetes objects")
    assert backup_sweeper.last_sweep(db_session)["errors"]


def test_sweeper_store_failure_and_last_sweep_defaults(db_session, runner, monkeypatch):
    assert backup_sweeper.last_sweep(db_session)["last_run_at"] is None
    db_session.add(models.PlatformSetting(key=backup_sweeper.STATE_KEY, value="{not json"))
    db_session.commit()
    assert backup_sweeper.last_sweep(db_session) == {"last_run_at": None, "cleaned": {}, "errors": [], "orphan_repos": []}
    monkeypatch.setattr(backup_sweeper, "_store", lambda *a: (_ for _ in ()).throw(RuntimeError("db")))
    assert "last_run_at" in backup_sweeper.sweep(db_session, runner, LATE)  # never raises


def test_sweeper_store_updates_existing_row_and_sheds_detail(db_session):
    big = {"last_run_at": "x", "cleaned": {k: 1 for k in backup_sweeper.CLEAN_KEYS}, "errors": ["e" * 80] * 3,
           "orphan_repos": list(range(100000000, 100000010))}
    backup_sweeper._store(db_session, big, NOW)
    backup_sweeper._store(db_session, {**big, "cleaned": {"pods": 2}}, NOW)
    row = db_session.get(models.PlatformSetting, backup_sweeper.STATE_KEY)
    assert len(row.value) <= 255 and "pods" in row.value
    stored = backup_sweeper.last_sweep(db_session)
    assert len(stored["orphan_repos"]) == 5 and all(len(e) <= 20 for e in stored["errors"])  # long-tail detail shed first
    assert db_session.query(models.PlatformSetting).filter_by(key=backup_sweeper.STATE_KEY).count() == 1


def test_sweeper_recent_delete_attempt_blocks_revert_but_old_one_reverts(db_session, aid, runner):
    sid = mkset(db_session, aid, 400)
    src = db_session.query(models.BackupRun).filter_by(set_id=sid).one()
    src.status = "deleting"
    db_session.add(models.BackupRun(app_id=aid, kind="db", trigger="delete", status="failed", source_run_id=src.id,
                                    created_at=LATE - timedelta(minutes=2), finished_at=LATE - timedelta(minutes=1)))
    db_session.commit()
    sweep(db_session, runner)
    db_session.expire_all()
    assert db_session.get(models.BackupRun, src.id).status == "deleting"  # a delete just failed: give the retry room
    sweep(db_session, runner, now=LATE + timedelta(hours=3))
    db_session.expire_all()
    assert db_session.get(models.BackupRun, src.id).status == "succeeded"


# --- backups: rolloff failure isolation, retention, start-time refusals ------------------------------------

def test_roll_off_skips_refused_sets_and_stops_on_unexpected_error(db_session, aid, runner, monkeypatch, caplog):
    s1, s2, s3 = (mkset(db_session, aid, m, status="failed") for m in (30, 20, 10))
    app = db_session.get(models.App, aid)
    calls = []

    def fake(session, app_, st, runner_, now=None):
        calls.append(st.id)
        if st.id == s1:
            raise backups.DeleteRefused("busy", 409)
        if st.id == s3:
            raise RuntimeError("db gone")
        return [], []

    monkeypatch.setattr(backups, "enqueue_delete_set", fake)
    with caplog.at_level(logging.WARNING):
        assert backups.roll_off(db_session, app, runner, 5, "max_held", NOW) == 1  # s2 done; s3 error stops the pass
    assert calls == [s1, s2, s3] and "roll-off" in caplog.text and "db gone" not in caplog.text
    assert [a.detail for a in rows(db_session, "backup.rolled_off")][0].startswith(f"set={s2} reason=max_held")
    assert backups.roll_off(db_session, app, runner, 0, "x", NOW) == 0


def test_enforce_retention_never_raises_and_skips_busy_apps(db_session, aid, runner, monkeypatch, caplog):
    for m in (50, 40, 30):
        mkset(db_session, aid, m)
    monkeypatch.setattr(config, "BACKUP_DEFAULT_ENABLED", True, raising=False)
    from app import platform_settings
    platform_settings.invalidate()
    db_session.add(models.PlatformSetting(key="backup_max_held_per_app", value="1"))
    db_session.commit()
    platform_settings.invalidate()
    run = models.BackupRun(app_id=aid, kind="db", trigger="manual", status="running", created_at=NOW, started_at=NOW)
    db_session.add(run)
    db_session.commit()
    backups._enforce_retention(db_session, runner, NOW)
    assert backups.held_count(db_session, aid) == 3  # a backup is in flight: nothing rolled
    run.status = "failed"
    db_session.commit()
    backups._enforce_retention(db_session, runner, NOW)
    assert backups.held_count(db_session, aid) == 2  # one per tick: it stops after the backup that needs a delete Job
    assert db_session.query(models.BackupRun).filter_by(app_id=aid, trigger="delete").count() == 1
    monkeypatch.setattr(platform_settings, "get", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    with caplog.at_level(logging.WARNING):
        backups._enforce_retention(db_session, runner, NOW)
    assert "retention pass failed" in caplog.text
    platform_settings.invalidate()


def test_queued_backup_refused_when_app_or_platform_full(db_session, aid, runner, monkeypatch):
    from tests.test_backups_coverage import _queue
    cfg = db_session.get(models.AppBackupConfig, aid)
    cfg.db_repo_bytes = backups.app_cap_bytes(db_session, cfg) + 1
    db_session.commit()
    r1 = _queue(db_session, aid, "db", "scheduled")
    backups.tick(db_session, runner, NOW)
    db_session.expire_all()
    assert db_session.get(models.BackupRun, r1.id).status == "failed" and runner.started == []
    assert db_session.get(models.BackupRun, r1.id).error == backups.app_full_msg(db_session, cfg)
    cfg.db_repo_bytes = 0
    cfg.admin_disabled = False
    db_session.commit()
    monkeypatch.setattr(backups, "total_repo_bytes", lambda s: backups.total_cap_bytes(s))
    r2 = _queue(db_session, aid, "db", "manual")
    backups.tick(db_session, runner, NOW)
    db_session.expire_all()
    assert db_session.get(models.BackupRun, r2.id).error == backups.PLATFORM_FULL_MSG


def test_admin_lock_blocks_scheduled_and_manual_at_enqueue_and_start(user, admin, db_session, aid, runner):
    r = admin.put(f"/admin/apps/{aid}/backups", json={"admin_disabled": True, "reason": "abuse"})
    assert r.status_code == 200, r.text
    assert user.post(f"/apps/{aid}/backups").status_code in (403, 409)
    from tests.test_backups_coverage import _queue
    run = _queue(db_session, aid, "db", "scheduled")
    backups.tick(db_session, runner, NOW)
    db_session.expire_all()
    assert db_session.get(models.BackupRun, run.id).error == backups.ADMIN_DISABLED_MSG and runner.started == []


# --- admin diag router error paths ---------------------------------------------------------------------

from tests.test_admin_health_diag import _admin, kube  # noqa: E402,F401
from app.log_redact import redact_line  # noqa: E402


def test_diag_pod_list_failure_is_502_without_detail(make_client, kube, monkeypatch):
    monkeypatch.setattr(kube, "platform_pods", lambda: (_ for _ in ()).throw(RuntimeError("secret-looking-detail")))
    r = _admin(make_client).get("/admin/health/logs/pods")
    assert r.status_code == 502 and "secret-looking-detail" not in r.text


def test_diag_pod_list_drops_non_platform_even_if_provider_returns_them(make_client, kube, monkeypatch):
    monkeypatch.setattr(kube, "platform_pods", lambda: [{"namespace": "app-9", "name": "x", "containers": []},
                                                        {"namespace": "traefik", "name": "t", "containers": ["c"]}])
    j = _admin(make_client).get("/admin/health/logs/pods").json()
    assert j["namespaces"] == ["traefik"] and [p["name"] for p in j["pods"]] == ["t"]


@pytest.mark.parametrize("exc,code", [(ValueError("x"), 403), (LookupError("x"), 404), (RuntimeError("secret-looking-detail"), 502)])
def test_diag_log_read_exceptions_map_to_status(make_client, kube, monkeypatch, exc, code):
    monkeypatch.setattr(kube, "platform_log", lambda *a: (_ for _ in ()).throw(exc))
    admin = _admin(make_client)
    for ep in ("logs", "logs/download"):
        r = admin.get(f"/admin/health/{ep}?namespace=traefik&pod=t-0&container=traefik")
        assert r.status_code == code and "secret-looking-detail" not in r.text


def test_diag_logs_tail_byte_budget_truncates_oldest_and_redacts(make_client, kube, monkeypatch):
    big = [f"{i:05d} " + "x" * 1000 for i in range(1000)]  # ~1 MB > the 512 KB view budget
    monkeypatch.setattr(kube, "platform_log", lambda *a: list(big))
    admin = _admin(make_client)
    j = admin.get("/admin/health/logs?namespace=traefik&pod=t-0&container=traefik&tail=1000").json()
    assert j["truncated"] is True and 400 < len(j["lines"]) < 600
    assert j["lines"][-1].startswith("00999 ")  # newest kept, oldest dropped
    monkeypatch.setattr(kube, "platform_log", lambda *a: ["Authorization: Bearer abcdef0123456789abcdef", "plain"])
    j = admin.get("/admin/health/logs?namespace=traefik&pod=t-0&container=traefik").json()
    assert j["truncated"] is False and j["lines"] == [redact_line("Authorization: Bearer abcdef0123456789abcdef"), "plain"]
    assert "abcdef0123456789abcdef" not in j["lines"][0]


def test_diag_download_empty_log_has_no_trailing_newline(make_client, kube, monkeypatch, db_session):
    monkeypatch.setattr(kube, "platform_log", lambda *a: [])
    r = _admin(make_client).get("/admin/health/logs/download?namespace=traefik&pod=t-0&container=traefik")
    assert r.status_code == 200 and r.text == "" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store" and 't-0' in r.headers["content-disposition"]


# --- BackupRunner base-class defaults ------------------------------------------------------------------

def test_backup_runner_defaults_are_inert():
    from app.providers.backup import BackupRunner, JobStatus

    class Min(BackupRunner):
        def start(self, spec): ...
        def status(self, job_name, namespace=None): return JobStatus("running")
        def cleanup(self, job_name, namespace=None): ...
        def sweep(self, active): return 0

    m = Min()
    assert m.ensure_spaces_egress("x") is None and m.sweep_exports(1) == [] and m.delete_exports(["k"]) is None
    assert m.sweep_namespaces(["a"], set()) == 0 and m.abort_stale_uploads(1, 1) == 0 and m.repo_app_ids() == set()
    assert m.sweep_orphans(["a"], set(), 1, 1) == {"jobs": 0, "pods": 0, "secrets": 0}
    with pytest.raises(BackupRunnerError):
        m.presign_export("k", 1, "f")
