"""Real per-client-IP limits (Traefik forwards the true client IP; the rightmost X-Forwarded-For entry is the peer it saw)."""
import pytest

from app import config, models, ratelimit
from tests.conftest import DEFAULT_PASSWORD, register_and_login

ROOT = "root-management-fixture-value-0123456789"
AUTH = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731
XFF = lambda ip: {"X-Forwarded-For": ip}  # noqa: E731
BOGUS = "pp_sk_" + "3" * 64
A, B = "198.51.100.10", "198.51.100.11"


@pytest.fixture
def limiter_on(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    ratelimit._invalid_pending.clear()
    yield
    ratelimit._invalid_pending.clear()


def _bad(c, ip, key=BOGUS):
    return c.get("/apps", headers={**AUTH(key), **XFF(ip)})


def _valid_key(make_client):
    c = make_client()
    register_and_login(c, "owner@example.com")
    return c.post("/api/keys").json()["key"]


def test_defaults():
    assert (config.LOGIN_IP_MAX_FAILS, config.REGISTER_IP_MAX, config.ROOT_KEY_MAX_FAILS) == (30, 10, 5)
    assert (config.INVALID_API_KEY_IP_MAX_PER_MINUTE, config.INVALID_API_KEY_IP_MAX_PER_HOUR) == (30, 300)


def test_invalid_keys_429_after_30_a_minute_other_ip_and_valid_key_unaffected(make_client, limiter_on, db_session):
    good = _valid_key(make_client)
    anon = make_client()
    assert [_bad(anon, A).status_code for _ in range(30)] == [401] * 30
    r = _bad(anon, A)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert _bad(anon, B).status_code == 401
    ok = anon.get("/apps", headers={**AUTH(good), **XFF(A)})
    assert ok.status_code == 200
    # routes using _user_from_request directly propagate the 429 too
    assert anon.get("/docs", headers={**AUTH(BOGUS), **XFF(A)}).status_code == 429
    # keys are hashed, never the IP or the bearer text
    for row in db_session.query(models.AuthThrottle).all():
        assert A not in row.key and "3333" not in row.key


def test_invalid_keys_hour_limit(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "INVALID_API_KEY_IP_MAX_PER_HOUR", 4)
    anon = make_client()
    assert [_bad(anon, A).status_code for _ in range(4)] == [401] * 4
    r = _bad(anon, A)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 60
    assert _bad(anon, B).status_code == 401


def test_valid_key_requests_write_no_invalid_ip_row(make_client, limiter_on, db_session):
    good = _valid_key(make_client)
    db_session.query(models.AuthThrottle).delete()
    db_session.commit()
    make_client().get("/apps", headers={**AUTH(good), **XFF(A)})
    keys = {r.key for r in db_session.query(models.AuthThrottle).all()}
    assert keys == {ratelimit._key("api-key-minute", str(db_session.query(models.ApiKey).one().id)),
                    ratelimit._key("api-key-hour", str(db_session.query(models.ApiKey).one().id))}


def test_global_invalid_counter_still_counts(make_client, limiter_on, db_session):
    anon = make_client()
    for _ in range(5):
        _bad(anon, A)
    _bad(anon, B)
    assert ratelimit.invalid_api_keys_last(db_session, 15) == 6


def test_invalid_key_fails_open_when_store_errors(make_client, limiter_on, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ratelimit, "_remaining", boom)
    assert _bad(make_client(), A).status_code == 401


def test_disabled_when_rate_limit_off(make_client, monkeypatch, db_session):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "INVALID_API_KEY_IP_MAX_PER_MINUTE", 1)
    anon = make_client()
    assert [_bad(anon, A).status_code for _ in range(4)] == [401] * 4
    assert db_session.query(models.AuthThrottle).count() == 0


def test_root_key_wrong_attempts_429_after_5_per_ip_right_key_works(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "ROOT_MANAGEMENT_KEY", ROOT)
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    anon = make_client()
    url = "/root/admin-reset"
    body = {"email": "admin@example.com"}
    wrong = {"Authorization": "Bearer wrong-key-wrong-key-wrong-key-wrong-key"}
    assert [anon.post(url, headers={**wrong, **XFF(A)}, json=body).status_code for _ in range(5)] == [401] * 5
    assert anon.post(url, headers={**wrong, **XFF(A)}, json=body).status_code == 429
    assert anon.post(url, headers={**wrong, **XFF(B)}, json=body).status_code == 401
    right = anon.post(url, headers={**AUTH(ROOT), **XFF(A)}, json=body)
    assert right.status_code == 200


def test_login_per_ip_limit_is_30(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_EMAIL_MAX_FAILS", 10**6)  # isolate the IP bucket
    c = make_client()
    for i in range(30):
        r = c.post("/auth/login", json={"email": f"nobody{i}@example.com", "password": "not-the-password"}, headers=XFF(A))
        assert r.status_code == 401
    r = c.post("/auth/login", json={"email": "nobody99@example.com", "password": "not-the-password"}, headers=XFF(A))
    assert r.status_code == 429
    r = c.post("/auth/login", json={"email": "nobody98@example.com", "password": "not-the-password"}, headers=XFF(B))
    assert r.status_code == 401


def test_register_per_ip_limit_is_10(make_client, limiter_on):
    c = make_client()
    def reg(i, ip):
        return c.post("/auth/register", json={"email": f"u{i}@example.com", "password": DEFAULT_PASSWORD,
                                              "accept_terms": True}, headers=XFF(ip))
    assert [reg(i, A).status_code for i in range(10)] == [201] * 10
    r = reg(10, A)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert reg(11, B).status_code == 201


def test_limits_are_env_overridable():
    import importlib
    import os
    env = {"LOGIN_IP_MAX_FAILS": "7", "REGISTER_IP_MAX": "3", "ROOT_KEY_MAX_FAILS": "2",
           "INVALID_API_KEY_IP_MAX_PER_MINUTE": "4", "INVALID_API_KEY_IP_MAX_PER_HOUR": "9"}
    old = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        c = importlib.reload(config)
        assert (c.LOGIN_IP_MAX_FAILS, c.REGISTER_IP_MAX, c.ROOT_KEY_MAX_FAILS,
                c.INVALID_API_KEY_IP_MAX_PER_MINUTE, c.INVALID_API_KEY_IP_MAX_PER_HOUR) == (7, 3, 2, 4, 9)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        importlib.reload(config)
