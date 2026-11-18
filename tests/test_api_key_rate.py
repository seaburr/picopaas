"""Per-API-key request budget and the global invalid-key counter (app/ratelimit.py)."""
import pytest

from app import config, models, ratelimit
from tests.conftest import register_and_login

AUTH = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731


@pytest.fixture
def limiter_on(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    # the per-IP invalid-key throttle has its own file (tests/test_ip_limits_2026_11_18.py); keep it out of the way here
    monkeypatch.setattr(config, "INVALID_API_KEY_IP_MAX_PER_MINUTE", 10**6)
    monkeypatch.setattr(config, "INVALID_API_KEY_IP_MAX_PER_HOUR", 10**6)
    ratelimit._invalid_pending.clear()  # in-memory batch is per process: start each test empty
    yield
    ratelimit._invalid_pending.clear()


def _user_with_key(make_client, email="owner@example.com", scope=None):
    c = make_client()
    register_and_login(c, email)
    key = c.post("/api/keys", json={"scope": scope} if scope else None).json()["key"]
    return c, key, make_client()


def _key_id(db_session, raw):
    from app.security import hash_api_key
    return db_session.query(models.ApiKey).filter(models.ApiKey.hashed == hash_api_key(raw)).one().id


def test_under_the_limit_passes(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "API_KEY_RATE_PER_MINUTE", 5)
    _, key, anon = _user_with_key(make_client)
    assert [anon.get("/apps", headers=AUTH(key)).status_code for _ in range(5)] == [200] * 5


def test_over_the_minute_limit_gets_429_with_retry_after(make_client, limiter_on, monkeypatch, db_session):
    monkeypatch.setattr(config, "API_KEY_RATE_PER_MINUTE", 3)
    _, key, anon = _user_with_key(make_client)
    for _ in range(3):
        assert anon.get("/apps", headers=AUTH(key)).status_code == 200
    r = anon.get("/apps", headers=AUTH(key))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert "This API key made too many requests" in r.json()["detail"] and "per minute" in r.json()["detail"]
    # routes calling _user_from_request directly also answer 429, not 401/404
    assert anon.get("/docs", headers=AUTH(key)).status_code == 429


def test_default_budget_is_120_a_minute_and_3000_an_hour():
    assert (config.API_KEY_RATE_PER_MINUTE, config.API_KEY_RATE_PER_HOUR) == (120, 3000)


def test_hour_limit(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "API_KEY_RATE_PER_HOUR", 2)
    _, key, anon = _user_with_key(make_client)
    assert anon.get("/apps", headers=AUTH(key)).status_code == 200
    assert anon.get("/apps", headers=AUTH(key)).status_code == 200
    r = anon.get("/apps", headers=AUTH(key))
    assert r.status_code == 429 and "per hour" in r.json()["detail"]


def test_separate_keys_have_separate_budgets(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "API_KEY_RATE_PER_MINUTE", 2)
    c, k1, anon = _user_with_key(make_client)
    k2 = c.post("/api/keys").json()["key"]
    for _ in range(2):
        anon.get("/apps", headers=AUTH(k1))
    assert anon.get("/apps", headers=AUTH(k1)).status_code == 429
    assert anon.get("/apps", headers=AUTH(k2)).status_code == 200


def test_browser_session_is_not_counted(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "API_KEY_RATE_PER_MINUTE", 2)
    c, _, _ = _user_with_key(make_client)
    assert [c.get("/apps").status_code for _ in range(6)] == [200] * 6


def test_read_scope_key_is_counted(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "API_KEY_RATE_PER_MINUTE", 2)
    _, key, anon = _user_with_key(make_client, scope="read")
    assert [anon.get("/apps", headers=AUTH(key)).status_code for _ in range(3)] == [200, 200, 429]


def test_audit_row_written_once_per_window(make_client, limiter_on, monkeypatch, db_session):
    monkeypatch.setattr(config, "API_KEY_RATE_PER_MINUTE", 1)
    _, key, anon = _user_with_key(make_client)
    for _ in range(5):
        anon.get("/apps", headers=AUTH(key))
    rows = db_session.query(models.AuditLog).filter(models.AuditLog.action == "api_key.rate_limited").all()
    assert len(rows) == 1
    owner = db_session.query(models.User).filter(models.User.email == "owner@example.com").one()
    assert rows[0].target_id == str(owner.id)
    assert rows[0].detail == f"key_id={_key_id(db_session, key)} window=minute"
    assert key not in (rows[0].detail or "")


def test_counter_is_keyed_by_id_not_key_text(make_client, limiter_on, db_session):
    _, key, anon = _user_with_key(make_client)
    anon.get("/apps", headers=AUTH(key))
    keys = [r.key for r in db_session.query(models.AuthThrottle).all()]
    assert keys and not any(key in k or k.startswith("pp_sk_") for k in keys)


def test_invalid_keys_are_counted_not_blocked_and_not_stored(make_client, limiter_on, db_session):
    anon = make_client()
    bogus = "pp_sk_" + "1" * 64
    for _ in range(300):  # far past any per-key limit: never blocked
        assert anon.get("/apps", headers=AUTH(bogus)).status_code == 401
    assert anon.get("/apps", headers=AUTH("random-bearer-text")).status_code == 401
    assert ratelimit.invalid_api_keys_last(db_session, 15) == 301
    assert ratelimit.invalid_api_keys_last(db_session, 1) == 301
    for row in db_session.query(models.AuthThrottle).all():
        assert "1111" not in row.key and "random-bearer" not in row.key
        assert row.key.startswith("invalid-api-key:global:") or len(row.key) == 64  # global row or hashed per-IP row


def test_prune_cutoff_covers_the_windows(db_session, monkeypatch):
    monkeypatch.setattr(ratelimit.random, "random", lambda: 0.0)
    from datetime import timedelta
    old = ratelimit._now() - timedelta(minutes=30)
    db_session.add(models.AuthThrottle(key="invalid-api-key:global:x", window_start=old, count=2))
    db_session.commit()
    ratelimit._prune(db_session)
    assert db_session.get(models.AuthThrottle, "invalid-api-key:global:x") is not None


def test_disabled_when_rate_limit_is_off(make_client, monkeypatch, db_session):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "API_KEY_RATE_PER_MINUTE", 1)
    _, key, anon = _user_with_key(make_client)
    assert [anon.get("/apps", headers=AUTH(key)).status_code for _ in range(4)] == [200] * 4
    anon.get("/apps", headers=AUTH("pp_sk_" + "2" * 64))
    assert db_session.query(models.AuthThrottle).count() == 0
    assert ratelimit.invalid_api_keys_last(db_session, 15) == 0


def test_invalid_key_flood_is_written_in_batches_not_per_request(make_client, limiter_on, db_session, monkeypatch):
    """K158: an anonymous flood of bad keys costs a few batched writes, not one write per request."""
    writes = []
    real = ratelimit._add_to_bucket
    monkeypatch.setattr(ratelimit, "_add_to_bucket", lambda db, key, n: (writes.append(n), real(db, key, n)))
    monkeypatch.setattr(ratelimit, "_invalid_last_flush", __import__("time").monotonic())  # a flush just happened
    anon = make_client()
    for _ in range(200):
        assert anon.get("/apps", headers=AUTH("pp_sk_" + "2" * 64)).status_code == 401
    assert len(writes) <= 3, writes  # at most a bucket change plus the 5-second timer during the loop
    assert ratelimit.invalid_api_keys_last(db_session, 2) == 200  # the read flushes what is still pending
