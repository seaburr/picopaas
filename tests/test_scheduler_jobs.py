"""app/scheduler.py: leader election, job wrappers, bootstrap sync, and job registration. Everything is faked:
no MySQL, no cluster, no scheduler threads."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app import config, crypto, models, naming, scheduler


@pytest.fixture(autouse=True)
def _reset_lock(monkeypatch):
    monkeypatch.setattr(scheduler, "_lock_conn", None)


@pytest.fixture()
def sess(db_session, monkeypatch):
    """Make the scheduler's SessionLocal hand out the shared test session and keep it open."""
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    return db_session


# --- leader election ---------------------------------------------------------------------------------------

class FakeConn:
    def __init__(self, got=1, fail=False):
        self.got, self.fail, self.commits, self.closed, self.calls = got, fail, 0, False, []

    def execute(self, stmt, params=None):
        if self.fail:
            raise RuntimeError("connection lost")
        self.calls.append((str(stmt), params))
        return SimpleNamespace(scalar=lambda: self.got)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, conns, dialect="mysql"):
        self.dialect = SimpleNamespace(name=dialect)
        self._conns = list(conns)
        self.connects = 0

    def connect(self):
        self.connects += 1
        return self._conns.pop(0)


def test_leader_acquired_reuses_one_connection(monkeypatch):
    conn = FakeConn(got=1)
    eng = FakeEngine([conn])
    monkeypatch.setattr(scheduler, "engine", eng)
    assert scheduler.try_acquire_leader() is True and scheduler.try_acquire_leader() is True
    assert eng.connects == 1 and conn.commits == 2  # named locks are per-connection: never reconnect needlessly
    assert conn.calls[0][1] == {"n": scheduler.LOCK_NAME} and "GET_LOCK" in conn.calls[0][0]


def test_not_leader_when_lock_held_elsewhere(monkeypatch):
    monkeypatch.setattr(scheduler, "engine", FakeEngine([FakeConn(got=0)]))
    assert scheduler.try_acquire_leader() is False


def test_lock_query_failure_drops_connection_and_reconnects_next_tick(monkeypatch, caplog):
    bad, good = FakeConn(fail=True), FakeConn(got=1)
    eng = FakeEngine([bad, good])
    monkeypatch.setattr(scheduler, "engine", eng)
    with caplog.at_level(logging.ERROR):
        assert scheduler.try_acquire_leader() is False  # never assume leadership on error
    assert bad.closed and scheduler._lock_conn is None
    assert scheduler.try_acquire_leader() is True and eng.connects == 2


def test_connect_failure_is_not_leader(monkeypatch):
    class Boom(FakeEngine):
        def connect(self):
            raise RuntimeError("no db")

    monkeypatch.setattr(scheduler, "engine", Boom([]))
    assert scheduler.try_acquire_leader() is False


# --- job wrappers ------------------------------------------------------------------------------------------

def test_jobs_do_nothing_when_not_leader(monkeypatch):
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: False)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: pytest.fail("a non-leader must not open a session"))
    for job in (scheduler.reconcile_job, scheduler.db_quota_job, scheduler.storage_quota_job,
                scheduler.quota_job, scheduler.image_watch_job, scheduler.metrics_sample_job):
        job()


def test_reconcile_job_passes_real_wiring_and_swallows_errors(sess, monkeypatch, caplog):
    seen = {}
    import app.providers.kube as kube_mod
    import app.reconciler as rec

    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    monkeypatch.setattr(kube_mod, "KubeClient", lambda: "KUBE")
    monkeypatch.setattr(scheduler, "_object_store", lambda: None)  # Spaces unconfigured -> _NoStorage

    def fake_reconcile_all(session, **kw):
        seen.update(kw, session=session)

    monkeypatch.setattr(rec, "reconcile_all", fake_reconcile_all)
    scheduler.reconcile_job()
    assert seen["session"] is sess and seen["kube"] == "KUBE" and isinstance(seen["object_store"], scheduler._NoStorage)
    assert seen["settings"].mysql_egress_mode == config.MYSQL_EGRESS_MODE

    def boom(*a, **k):
        raise RuntimeError("cluster down")

    monkeypatch.setattr(rec, "reconcile_all", boom)
    with caplog.at_level(logging.ERROR):
        scheduler.reconcile_job()  # must not raise: the scheduler thread has to survive
    assert "reconcile_all failed" in caplog.text


@pytest.mark.parametrize("job,expect", [
    ("db_quota_job", dict(databases=True, storage=False)),
    ("storage_quota_job", dict(databases=False, storage=True)),
    ("quota_job", dict(databases=True, storage=True)),
])
def test_quota_jobs_select_the_right_polls(sess, monkeypatch, job, expect):
    import app.quota as quota

    got = {}
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    monkeypatch.setattr(scheduler, "_object_store", lambda: None)
    monkeypatch.setattr(quota, "poll_quotas", lambda session, **kw: got.update(kw))
    getattr(scheduler, job)()
    assert {k: got[k] for k in ("databases", "storage")} == expect
    assert isinstance(got["object_store"], scheduler._NoStorage)


def test_quota_job_swallows_poll_errors(sess, monkeypatch, caplog):
    import app.quota as quota

    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    monkeypatch.setattr(scheduler, "_object_store", lambda: None)
    monkeypatch.setattr(quota, "poll_quotas", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with caplog.at_level(logging.ERROR):
        scheduler.quota_job()
    assert "poll_quotas failed" in caplog.text


def test_image_watch_job_logs_only_when_something_changed(sess, monkeypatch, caplog):
    from app import image_check

    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    results = iter([{"updated": 0, "rejected": 0}, {"updated": 1, "rejected": 0}])
    monkeypatch.setattr(image_check, "watch_all", lambda s, r: next(results))
    with caplog.at_level(logging.INFO):
        scheduler.image_watch_job()
        assert "image watch" not in caplog.text
        scheduler.image_watch_job()
        assert "image watch" in caplog.text
    monkeypatch.setattr(image_check, "watch_all", lambda s, r: (_ for _ in ()).throw(RuntimeError("registry down")))
    scheduler.image_watch_job()  # swallowed


def test_metrics_job_rolls_back_on_failure(sess, monkeypatch):
    from app import health

    rolled = []
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    monkeypatch.setattr("app.dependencies.get_kube", lambda: "KUBE")
    monkeypatch.setattr(sess, "rollback", lambda: rolled.append(1))
    monkeypatch.setattr(health, "sample_app_metrics", lambda s, k: (_ for _ in ()).throw(RuntimeError("x")))
    scheduler.metrics_sample_job()
    assert rolled == [1]


# --- object store / helpers --------------------------------------------------------------------------------

def test_no_storage_fails_loudly_on_use_not_on_construction():
    ns = scheduler._NoStorage()
    with pytest.raises(RuntimeError, match="unavailable"):
        ns.create_bucket_and_key(1)
    with pytest.raises(RuntimeError):
        ns.delete(1)


def test_mysql_factory_builds_a_client_per_server(monkeypatch):
    import app.providers.mysql as m

    monkeypatch.setattr(m, "MySQLAdminClient", lambda server: ("client", server))
    assert scheduler._mysql_factory("srv") == ("client", "srv")


def test_settings_reflect_config(monkeypatch):
    monkeypatch.setattr(config, "MYSQL_EGRESS_MODE", "cidr")
    monkeypatch.setattr(config, "MYSQL_EGRESS_CIDR", "")
    s = scheduler._settings()
    assert s.mysql_egress_mode == "cidr" and s.mysql_cidr is None
    monkeypatch.setattr(config, "MYSQL_EGRESS_CIDR", "10.0.0.0/16")
    assert scheduler._settings().mysql_cidr == "10.0.0.0/16"


def test_read_ca(monkeypatch, tmp_path):
    monkeypatch.delenv("MYSQL_SSL_CA", raising=False)
    monkeypatch.setattr(config, "MYSQL_SSL_CA", "")
    assert scheduler._read_ca() is None
    pem = tmp_path / "ca.pem"
    pem.write_text("  -----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----  \n\n")
    monkeypatch.setenv("MYSQL_SSL_CA", str(pem))
    assert scheduler._read_ca().endswith("-----END CERTIFICATE-----\n")
    monkeypatch.setenv("MYSQL_SSL_CA", str(tmp_path / "missing.pem"))
    assert scheduler._read_ca() is None  # unreadable -> None, never an exception at startup


def test_rotate_encrypted_data_never_blocks_startup(sess, monkeypatch, caplog):
    called = []
    monkeypatch.setattr(crypto, "rotate_all", lambda s: called.append(s) or {})
    scheduler.rotate_encrypted_data()
    assert called == [sess]
    monkeypatch.setattr(crypto, "rotate_all", lambda s: (_ for _ in ()).throw(RuntimeError("bad key")))
    with caplog.at_level(logging.ERROR):
        scheduler.rotate_encrypted_data()
    assert "fernet rotation failed" in caplog.text


# --- bootstrap_db_server -----------------------------------------------------------------------------------

def _env(monkeypatch, **kw):
    for k in ("BOOTSTRAP_DB_HOST", "BOOTSTRAP_DB_USER", "BOOTSTRAP_DB_PASSWORD", "BOOTSTRAP_DB_PORT", "MYSQL_SSL_CA"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(config, "MYSQL_SSL_CA", "")


@pytest.mark.parametrize("missing", ["BOOTSTRAP_DB_HOST", "BOOTSTRAP_DB_USER", "BOOTSTRAP_DB_PASSWORD"])
def test_bootstrap_requires_all_three(sess, monkeypatch, missing):
    env = dict(BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="pw")
    env.pop(missing)
    _env(monkeypatch, **env)
    scheduler.bootstrap_db_server()
    assert sess.query(models.DbServer).count() == 0


def test_bootstrap_default_port_and_ca(sess, monkeypatch, tmp_path):
    pem = tmp_path / "ca.pem"
    pem.write_text("PEM")
    _env(monkeypatch, BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="pw", MYSQL_SSL_CA=str(pem))
    scheduler.bootstrap_db_server()
    row = sess.query(models.DbServer).one()
    assert row.port == 3306 and row.ca_cert == "PEM\n"


def _seed_primary(sess, **kw):
    fields = dict(name="primary", host="old", port=3306, admin_user="old", max_databases=25,
                  admin_password_encrypted=crypto.encrypt("old-pw"))
    fields.update(kw)
    sess.add(models.DbServer(**fields))
    sess.commit()


def test_bootstrap_syncs_changed_connection_and_password(sess, monkeypatch):
    _seed_primary(sess)
    _env(monkeypatch, BOOTSTRAP_DB_HOST="newhost", BOOTSTRAP_DB_USER="newuser", BOOTSTRAP_DB_PASSWORD="new-pw",
         BOOTSTRAP_DB_PORT="3307")
    scheduler.bootstrap_db_server()
    row = sess.query(models.DbServer).one()
    assert (row.host, row.port, row.admin_user) == ("newhost", 3307, "newuser")
    assert crypto.decrypt(row.admin_password_encrypted) == "new-pw"


def test_bootstrap_repairs_undecryptable_password(sess, monkeypatch):
    _seed_primary(sess, admin_password_encrypted="not-a-valid-token")
    _env(monkeypatch, BOOTSTRAP_DB_HOST="old", BOOTSTRAP_DB_USER="old", BOOTSTRAP_DB_PASSWORD="pw")
    scheduler.bootstrap_db_server()
    assert crypto.decrypt(sess.query(models.DbServer).one().admin_password_encrypted) == "pw"


def test_bootstrap_fills_missing_ca_but_never_overwrites_one(sess, monkeypatch, tmp_path):
    pem = tmp_path / "ca.pem"
    pem.write_text("NEWCA")
    _seed_primary(sess, ca_cert=None)
    _env(monkeypatch, BOOTSTRAP_DB_HOST="old", BOOTSTRAP_DB_USER="old", BOOTSTRAP_DB_PASSWORD="old-pw",
         MYSQL_SSL_CA=str(pem))
    scheduler.bootstrap_db_server()
    row = sess.query(models.DbServer).one()
    assert row.ca_cert == "NEWCA\n"
    row.ca_cert = "ADMIN-SET"
    sess.commit()
    scheduler.bootstrap_db_server()
    assert sess.query(models.DbServer).one().ca_cert == "ADMIN-SET"


def test_bootstrap_leaves_unrelated_servers_alone(sess, monkeypatch):
    _seed_primary(sess, name="secondary", host="keep")
    _env(monkeypatch, BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="pw")
    scheduler.bootstrap_db_server()  # a server exists but none is named 'primary': nothing inserted or modified
    row = sess.query(models.DbServer).one()
    assert row.name == "secondary" and row.host == "keep"


def test_bootstrap_noop_when_already_in_sync(sess, monkeypatch, caplog):
    _seed_primary(sess, host="h", admin_user="u", admin_password_encrypted=crypto.encrypt("pw"))
    _env(monkeypatch, BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="pw")
    with caplog.at_level(logging.INFO):
        scheduler.bootstrap_db_server()
    assert "updated DbServer" not in caplog.text


def test_bootstrap_never_logs_the_password(sess, monkeypatch, caplog):
    _env(monkeypatch, BOOTSTRAP_DB_HOST="h", BOOTSTRAP_DB_USER="u", BOOTSTRAP_DB_PASSWORD="not-the-password-xyz")
    with caplog.at_level(logging.DEBUG):
        scheduler.bootstrap_db_server()
        _seed = sess.query(models.DbServer).one()
        _seed.admin_user = "changed"
        sess.commit()
        scheduler.bootstrap_db_server()
    assert "not-the-password-xyz" not in caplog.text


# --- start_scheduler ---------------------------------------------------------------------------------------

def test_start_scheduler_registers_all_jobs_once_each(monkeypatch):
    from app import health

    jobs, started = {}, []

    class FakeSched:
        def add_job(self, fn, trigger, id=None, **kw):
            jobs[id] = (fn, trigger, kw)

        def start(self):
            started.append(True)

    monkeypatch.setattr(scheduler, "BackgroundScheduler", FakeSched)
    monkeypatch.setattr(health, "start_background", lambda getter: started.append("health"))
    sched = scheduler.start_scheduler()
    assert isinstance(sched, FakeSched) and started == ["health", True]
    assert set(jobs) == {"reconcile", "quota_db", "quota_storage", "image_watch", "health", "metrics_sample"}
    for fn, trigger, kw in jobs.values():
        assert trigger == "interval" and kw["max_instances"] == 1 and kw["coalesce"] is True  # never overlap itself
    assert jobs["reconcile"][2]["seconds"] == 15 and jobs["reconcile"][0] is scheduler.reconcile_job
    # Quota polls run immediately at start so a redeploy doesn't delay enforcement a full interval.
    assert jobs["quota_db"][2]["next_run_time"] is not None and jobs["quota_storage"][2]["next_run_time"] is not None
    assert jobs["image_watch"][2]["next_run_time"] > jobs["quota_db"][2]["next_run_time"]


# --- naming: hostname generation exhaustion ----------------------------------------------------------------

def test_generate_unique_hostname_retries_then_gives_up(db_session, monkeypatch):
    seq = iter(["taken-abc-1234567"] * 3 + ["free-abc-7654321"])
    monkeypatch.setattr(naming, "_random_hostname", lambda: next(seq))
    owner = models.User(email="n@example.com", password_hash="x")
    db_session.add(owner)
    db_session.commit()
    db_session.add(models.App(owner_id=owner.id, name="a", hostname="taken-abc-1234567", image="i", port=8080))
    db_session.commit()
    assert naming.generate_unique_hostname(db_session) == "free-abc-7654321"
    monkeypatch.setattr(naming, "_random_hostname", lambda: "taken-abc-1234567")
    with pytest.raises(RuntimeError, match="unique hostname"):
        naming.generate_unique_hostname(db_session)
