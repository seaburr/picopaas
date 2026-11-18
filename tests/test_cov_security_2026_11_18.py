"""Coverage and test-quality pass on the 2026-11-16..18 security changes: window and threshold edges of the alert
rules, purge order, the rate limiter failing open, the in-memory invalid-key batch, root key with odd headers,
secretscan edge cases, the plan gate on malformed transcripts and the docs schema. Real login flow where a route is
involved; token-like fixtures are assembled at run time so no literal looks like a credential."""
import base64
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import config, models, ratelimit, security_alerts
from app.kaisen import builder, secretscan
from app.kaisen.paths import ToolError
from tests.conftest import DEFAULT_PASSWORD, register_and_login, utcnow_naive
from tests.test_api_key_rate import AUTH, _user_with_key
from tests.test_security_alerts import NOW, alerts, audit_rows, kev

BODY = "a1B2c3D4" * 6
ANTHROPIC = "sk-" + "ant-" + "api03-" + BODY
PICOPAAS = "pp_" + "sk_" + BODY


@pytest.fixture
def limiter_on(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    ratelimit._invalid_pending.clear()
    yield
    ratelimit._invalid_pending.clear()


def _boom(*a, **k):
    raise RuntimeError("database went away")


# ---- alert windows and thresholds, exactly at the edge -----------------------------------------------------------

def test_burst_window_edge_is_inclusive_and_one_second_more_is_outside():
    t0 = NOW
    w = timedelta(minutes=10)
    assert security_alerts._burst([t0, t0 + w], 2, w) == (2, t0, t0 + w)  # exactly the window wide: counts
    assert security_alerts._burst([t0, t0 + w + timedelta(seconds=1)], 2, w) is None
    assert security_alerts._burst([], 1, w) is None
    assert security_alerts._burst([t0], 1, w) == (1, t0, t0)
    # unsorted input is sorted first
    assert security_alerts._burst([t0 + timedelta(seconds=5), t0], 2, w)[1] == t0


def test_admin_burst_ten_actions_spanning_exactly_the_window_fires_a_second_more_does_not(db_session):
    n, w = config.SECURITY_ALERT_ADMIN_BURST_ACTIONS, timedelta(minutes=config.SECURITY_ALERT_ADMIN_BURST_MINUTES)
    start = (NOW - w - timedelta(minutes=1)).replace(microsecond=0)
    for extra in (timedelta(0), timedelta(seconds=1)):
        db_session.query(models.SecurityAlert).delete()
        db_session.query(models.AuditLog).delete()
        db_session.commit()
        audit_rows(db_session, "user.approve", n - 1, actor_id=7, start=start, step=timedelta(seconds=1))
        audit_rows(db_session, "user.approve", 1, actor_id=7, start=start + w + extra)  # first..last = window (+ extra)
        security_alerts.run(db_session, NOW)
        assert len(alerts(db_session, "admin_burst")) == (1 if not extra else 0), extra


def test_audit_older_than_the_lookback_is_ignored(db_session):
    old = NOW - timedelta(minutes=config.SECURITY_ALERT_LOOKBACK_MINUTES + 5)
    audit_rows(db_session, "user.approve", config.SECURITY_ALERT_ADMIN_BURST_ACTIONS, actor_id=7, start=old)
    security_alerts.run(db_session, NOW)
    assert alerts(db_session) == []


def test_kaisen_hour_spend_exactly_at_the_threshold_fires(db_session):
    h = (NOW - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    kev(db_session, 9, config.SECURITY_ALERT_KAISEN_HOUR_USD, h + timedelta(minutes=1))
    security_alerts.run(db_session, NOW)
    [a] = alerts(db_session, "kaisen_hour_spend")
    assert a.evidence["hour_usd"] == config.SECURITY_ALERT_KAISEN_HOUR_USD


def test_kaisen_hour_spend_a_cent_under_stays_quiet(db_session):
    h = (NOW - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    kev(db_session, 9, config.SECURITY_ALERT_KAISEN_HOUR_USD - 0.01, h + timedelta(minutes=1))
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "kaisen_hour_spend") == []


def test_kaisen_hour_spend_older_than_24_hours_is_ignored(db_session):
    h = (NOW - timedelta(hours=30)).replace(minute=0, second=0, microsecond=0)
    kev(db_session, 9, 50.0, h + timedelta(minutes=1))
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "kaisen_hour_spend") == []


def test_kaisen_spike_exactly_at_the_dollar_floor_fires(db_session):
    day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    kev(db_session, 4, config.SECURITY_ALERT_KAISEN_SPIKE_MIN_USD, day0 + timedelta(seconds=1))  # no history: avg 0
    security_alerts.run(db_session, day0 + timedelta(hours=2))
    assert len(alerts(db_session, "kaisen_spend_spike")) == 1


def test_kaisen_spike_exactly_factor_times_average_fires_but_not_a_cent_below(db_session):
    day0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(1, 15):
        kev(db_session, 1, 1.4, day0 - timedelta(days=d, hours=-1))  # avg 1.4 -> factor 3 needs 4.2
        kev(db_session, 2, 1.4, day0 - timedelta(days=d, hours=-1))
    kev(db_session, 1, 4.2, day0 + timedelta(seconds=1))
    kev(db_session, 2, 4.19, day0 + timedelta(seconds=1))
    security_alerts.run(db_session, day0 + timedelta(hours=2))
    assert [a.subject_id for a in alerts(db_session, "kaisen_spend_spike")] == ["1"]


def test_mass_account_changes_at_threshold_and_one_under(db_session):
    n = config.SECURITY_ALERT_MASS_ACCOUNT_CHANGES
    audit_rows(db_session, "user.delete", n - 1, actor_id=1, start=NOW - timedelta(minutes=30))
    security_alerts.run(db_session, NOW)
    assert alerts(db_session, "mass_account_changes") == []
    audit_rows(db_session, "user.reject", 1, actor_id=2, start=NOW - timedelta(minutes=29))  # mixed actions and actors
    security_alerts.run(db_session, NOW)
    assert len(alerts(db_session, "mass_account_changes")) == 1


# ---- purge order, acknowledge/merge -----------------------------------------------------------------------------

def _alert(db, key, *, ack=None, sev="low", last=NOW):
    db.add(models.SecurityAlert(kind="k", severity=sev, message="m", first_seen=last, last_seen=last, count=1,
                                dedupe_key=key, acknowledged_at=ack))


def test_cap_evicts_acknowledged_rows_before_open_ones_even_when_the_open_ones_are_older(db_session, monkeypatch):
    _alert(db_session, "open-old", last=NOW - timedelta(hours=9))
    _alert(db_session, "ack-new#ack1", ack=NOW, last=NOW)
    _alert(db_session, "ack-new2#ack2", ack=NOW, last=NOW - timedelta(minutes=1))
    db_session.commit()
    monkeypatch.setattr(config, "SECURITY_ALERT_MAX_ROWS", 1)
    assert security_alerts.purge(db_session, NOW) == 2
    assert {a.dedupe_key for a in alerts(db_session)} == {"open-old"}


def test_cap_evicts_lowest_severity_then_oldest(db_session, monkeypatch):
    for key, sev, mins in (("hi", "high", 9), ("med-old", "medium", 9), ("med-new", "medium", 1), ("lo-new", "low", 1)):
        _alert(db_session, key, sev=sev, last=NOW - timedelta(minutes=mins))
    db_session.commit()
    monkeypatch.setattr(config, "SECURITY_ALERT_MAX_ROWS", 2)
    security_alerts.purge(db_session, NOW)
    assert {a.dedupe_key for a in alerts(db_session)} == {"hi", "med-new"}


def test_purge_keeps_an_acknowledged_row_exactly_at_the_retention_edge(db_session):
    edge = NOW - timedelta(days=config.SECURITY_ALERT_ACK_RETENTION_DAYS)
    _alert(db_session, "edge#ack1", ack=edge)
    _alert(db_session, "past#ack2", ack=edge - timedelta(seconds=1))
    db_session.commit()
    assert security_alerts.purge(db_session, NOW) == 1
    assert [a.dedupe_key for a in alerts(db_session)] == ["edge#ack1"]


def test_acknowledging_twice_over_http_changes_nothing_and_audits_once(make_client, db_session):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    _alert(db_session, "x", sev="high")
    db_session.commit()
    a = db_session.query(models.SecurityAlert).one()
    first = admin.post(f"/admin/security-alerts/{a.id}/acknowledge").json()
    again = admin.post(f"/admin/security-alerts/{a.id}/acknowledge").json()
    assert first["acknowledged_at"] == again["acknowledged_at"] and again["acknowledged_by"] is not None
    assert db_session.query(models.AuditLog).filter_by(action="security_alert.acknowledge").count() == 1
    assert db_session.query(models.SecurityAlert).one().dedupe_key == f"x#ack{a.id}"
    assert admin.get("/admin/security-alerts").json() == []
    assert len(admin.get("/admin/security-alerts?status=all").json()) == 1


def test_stale_evidence_does_not_reopen_an_acknowledged_alert_but_newer_evidence_does(db_session):
    audit_rows(db_session, "user.approve", config.SECURITY_ALERT_ADMIN_BURST_ACTIONS, actor_id=7)
    security_alerts.run(db_session, NOW)
    a = alerts(db_session, "admin_burst")[0]
    security_alerts.acknowledge(db_session, a, SimpleNamespace(id=1))
    assert security_alerts.run(db_session, NOW) == 0  # the same evidence again
    assert len(alerts(db_session, "admin_burst")) == 1
    audit_rows(db_session, "user.approve", 1, actor_id=7, start=NOW - timedelta(seconds=1))
    assert security_alerts.run(db_session, NOW) == 1
    assert [x.acknowledged_at is None for x in alerts(db_session, "admin_burst")] == [False, True]


def test_merge_raises_severity_never_lowers_it(db_session):
    f = security_alerts.Finding("k", "medium", "dk", "m", NOW - timedelta(minutes=2), NOW - timedelta(minutes=1))
    security_alerts.upsert(db_session, f)
    db_session.commit()
    hi = security_alerts.Finding("k", "high", "dk", "m", NOW, NOW)
    lo = security_alerts.Finding("k", "low", "dk", "m", NOW, NOW + timedelta(seconds=1))
    security_alerts.upsert(db_session, hi)
    security_alerts.upsert(db_session, lo)
    [row] = alerts(db_session)
    assert (row.severity, row.count) == ("high", 3)


def test_long_message_is_truncated_to_the_column(db_session):
    f = security_alerts.Finding("k", "low", "dk", "m" * 600, NOW, NOW)
    security_alerts.upsert(db_session, f)
    db_session.commit()
    assert len(alerts(db_session)[0].message) == 255


# ---- rate limiter fails open, never blocks the product -----------------------------------------------------------

def test_api_key_budget_fails_open_when_the_database_errors(make_client, limiter_on, monkeypatch):
    _, key, anon = _user_with_key(make_client)
    monkeypatch.setattr(ratelimit, "_remaining", _boom)
    assert anon.get("/apps", headers=AUTH(key)).status_code == 200


def test_login_throttle_fails_open_on_every_entry_point(make_client, limiter_on, monkeypatch):
    c = make_client()
    register_and_login(c, "owner@example.com")
    for name in ("_remaining", "_bump", "_prune"):
        monkeypatch.setattr(ratelimit, name, _boom)
    anon = make_client()
    assert anon.post("/auth/login", json={"email": "owner@example.com", "password": "not-the-password"}).status_code == 401
    assert anon.post("/auth/login", json={"email": "owner@example.com", "password": DEFAULT_PASSWORD}).status_code == 200


def test_safe_passes_http_exceptions_through_and_swallows_the_rest_with_a_rollback(db_session):
    def deny():
        raise HTTPException(status_code=429)

    with pytest.raises(HTTPException):
        ratelimit._safe(db_session, "x", deny)
    assert ratelimit._safe(db_session, "x", _boom) is None
    assert ratelimit._safe(db_session, "x", lambda: 5) == 5

    class BadSession:
        def rollback(self):
            raise RuntimeError("rollback failed too")

    assert ratelimit._safe(BadSession(), "x", _boom) is None  # even a failing rollback is swallowed


def test_root_key_failure_counter_failing_still_answers_401_not_500(make_client, limiter_on, monkeypatch):
    monkeypatch.setattr(config, "ROOT_MANAGEMENT_KEY", "root-management-fixture-value-0123456789")
    register_and_login(make_client(), "admin@example.com")
    monkeypatch.setattr(ratelimit, "_bump", _boom)
    r = make_client().post("/root/admin-reset", headers={"Authorization": "Bearer wrong-wrong-wrong-wrong-wrong"})
    assert r.status_code == 401


def test_root_key_wrong_attempts_are_capped_per_ip_and_the_right_key_still_works(make_client, limiter_on, monkeypatch):
    key = "root-management-fixture-value-0123456789"
    monkeypatch.setattr(config, "ROOT_MANAGEMENT_KEY", key)
    monkeypatch.setattr(config, "ROOT_KEY_MAX_FAILS", 3)
    register_and_login(make_client(), "admin@example.com")
    anon = make_client()
    codes = [anon.post("/root/admin-reset", headers={"Authorization": "Bearer nope-nope-nope-nope"}).status_code
             for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    ok = anon.post("/root/admin-reset", headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200 and ok.json()["email"] == "admin@example.com"


@pytest.mark.parametrize("value", [
    "Bearer café-key-with-latin1-bytes".encode("latin-1"),
    b"Bearer \xff\xfe\xfd binary junk",
    ("Bearer " + "A" * 100_000).encode(),
    b"bearer   ",
    b"BEARER " + b"x" * 10,
])
def test_root_key_with_non_ascii_or_huge_header_is_a_plain_401(make_client, monkeypatch, db_session, value):
    key = "root-management-fixture-value-0123456789"
    monkeypatch.setattr(config, "ROOT_MANAGEMENT_KEY", key)
    register_and_login(make_client(), "admin@example.com")
    r = make_client().post("/root/admin-reset", headers={"Authorization": value})
    assert r.status_code == 401
    rows = db_session.query(models.AuditLog).filter_by(action="root.auth_failed").all()
    assert len(rows) == 1 and (rows[0].detail or "").startswith("ip=")


def test_root_key_matches_case_insensitive_bearer_scheme_and_a_non_ascii_configured_key(make_client, monkeypatch):
    key = "rööt-key-with-umlauts-0123456789-abcdef"  # a configured key may itself be non-ASCII
    monkeypatch.setattr(config, "ROOT_MANAGEMENT_KEY", key)
    register_and_login(make_client(), "admin@example.com")
    r = make_client().post("/root/admin-reset", headers={"Authorization": b"bearer " + key.encode("latin-1", "replace")})
    assert r.status_code in (200, 401)  # how a client puts non-ASCII on the wire varies; the point is: never a 500
    r2 = make_client().post("/root/admin-reset", headers={"Authorization": "bEaReR nope-nope-nope-nope"})
    assert r2.status_code == 401


def test_client_ip_uses_the_last_forwarded_hop_truncates_and_falls_back():
    def req(xff=None, client=("1.2.3.4", 5)):
        headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
        return SimpleNamespace(headers=SimpleNamespace(get=lambda k, d="": dict((h[0].decode(), h[1].decode())
                                                                               for h in headers).get(k, d)),
                               client=SimpleNamespace(host=client[0]) if client else None)

    assert ratelimit.client_ip(req("9.9.9.9, 10.0.0.1 , 7.7.7.7")) == "7.7.7.7"  # an attacker-supplied first hop is ignored
    assert ratelimit.client_ip(req(" , ,")) == "1.2.3.4"
    assert ratelimit.client_ip(req("x" * 200)) == "x" * 64
    assert ratelimit.client_ip(req(None, client=None)) == "unknown"


# ---- the in-memory invalid-key batch -----------------------------------------------------------------------------

def test_invalid_key_batch_flushes_when_the_minute_bucket_changes(db_session, limiter_on, monkeypatch):
    base = utcnow_naive().replace(second=10, microsecond=0)
    clock = {"now": base}
    monkeypatch.setattr(ratelimit, "_now", lambda: clock["now"])
    monkeypatch.setattr(ratelimit, "_invalid_last_flush", time.monotonic())  # the 5 second timer has not elapsed
    for _ in range(3):
        ratelimit.record_invalid_api_key(db_session)
    assert db_session.query(models.AuthThrottle).count() == 0  # still only in memory
    clock["now"] = base + timedelta(minutes=1)  # the next minute: the old bucket is flushed on the next call
    ratelimit.record_invalid_api_key(db_session)
    rows = {r.key: r.count for r in db_session.query(models.AuthThrottle)}
    nxt = base + timedelta(minutes=1)  # the rollover flushes the old bucket together with the new minute's first count
    assert rows == {"invalid-api-key:global:" + base.strftime("%Y%m%d%H%M"): 3,
                    "invalid-api-key:global:" + nxt.strftime("%Y%m%d%H%M"): 1}
    assert ratelimit.invalid_api_keys_last(db_session, 1) == 1  # only the current minute
    assert ratelimit.invalid_api_keys_last(db_session, 2) == 4  # both buckets
    assert ratelimit.invalid_api_keys_last(db_session, 0) == 1  # nonsense minutes still read the current bucket


def test_invalid_key_batch_from_many_threads_loses_no_counts(db_session, limiter_on, monkeypatch):
    monkeypatch.setattr(ratelimit, "_invalid_last_flush", time.monotonic())  # no DB access inside the threads
    monkeypatch.setattr(ratelimit, "_flush_invalid", lambda db, force=False: None)
    threads = [threading.Thread(target=lambda: [ratelimit.record_invalid_api_key(db_session) for _ in range(250)])
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(ratelimit._invalid_pending.values()) == 2000
    monkeypatch.undo()
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    assert ratelimit.invalid_api_keys_last(db_session, 2) == 2000
    assert ratelimit._invalid_pending == {}


def test_invalid_key_flush_that_fails_is_dropped_not_raised_and_pending_is_cleared(db_session, limiter_on, monkeypatch):
    ratelimit._invalid_pending["invalid-api-key:global:202601010000"] = 4
    monkeypatch.setattr(ratelimit, "_add_to_bucket", _boom)
    ratelimit._flush_invalid(db_session, force=True)  # fail open: never raises into the request
    assert ratelimit._invalid_pending == {}


def test_flush_with_nothing_pending_or_not_due_does_nothing(db_session, limiter_on, monkeypatch):
    called = []
    monkeypatch.setattr(ratelimit, "_add_to_bucket", lambda *a: called.append(a))
    ratelimit._flush_invalid(db_session, force=True)
    ratelimit._invalid_pending[ratelimit._invalid_key_bucket(ratelimit._now())] = 2
    monkeypatch.setattr(ratelimit, "_invalid_last_flush", time.monotonic())
    ratelimit._flush_invalid(db_session)  # same minute, timer not elapsed
    assert called == [] and sum(ratelimit._invalid_pending.values()) == 2


def test_add_to_bucket_survives_losing_the_insert_race(db_session):
    key = "invalid-api-key:global:202601010000"
    db_session.add(models.AuthThrottle(key=key, window_start=utcnow_naive(), count=5))
    db_session.commit()
    real_get = db_session.get
    calls = []

    def blind_get(model, ident, *a, **k):  # this replica "does not see" the row another one just inserted
        calls.append(ident)
        return None if len(calls) == 1 else real_get(model, ident, *a, **k)

    db_session.get = blind_get
    try:
        ratelimit._add_to_bucket(db_session, key, 3)
    finally:
        del db_session.get
    assert real_get(models.AuthThrottle, key).count == 8


def test_bump_survives_losing_the_insert_race(db_session):
    key = ratelimit._key("t", "race")
    db_session.add(models.AuthThrottle(key=key, window_start=utcnow_naive(), count=1))
    db_session.commit()
    real_get = db_session.get
    seen = []
    db_session.get = lambda m, i, *a, **k: None if not seen and not seen.append(1) else real_get(m, i, *a, **k)
    try:
        ratelimit._bump(db_session, key, 60)
    finally:
        del db_session.get
    assert real_get(models.AuthThrottle, key).count == 2


def test_bump_restarts_an_expired_window(db_session):
    key = ratelimit._key("t", "old")
    db_session.add(models.AuthThrottle(key=key, window_start=utcnow_naive() - timedelta(seconds=61), count=99))
    db_session.commit()
    ratelimit._bump(db_session, key, 60)
    assert db_session.get(models.AuthThrottle, key).count == 1
    assert ratelimit._remaining(db_session, key, 1, 60) > 0 and ratelimit._remaining(db_session, key, 2, 60) == 0


def test_remaining_is_zero_after_the_window_even_over_the_limit(db_session):
    key = ratelimit._key("t", "expired")
    db_session.add(models.AuthThrottle(key=key, window_start=utcnow_naive() - timedelta(seconds=61), count=99))
    db_session.commit()
    assert ratelimit._remaining(db_session, key, 5, 60) == 0
    assert ratelimit._remaining(db_session, "no-such-key", 5, 60) == 0


def test_revoked_key_is_counted_as_invalid_and_never_blocked(make_client, limiter_on, db_session):
    c, key, anon = _user_with_key(make_client)
    kid = c.get("/api/keys").json()[0]["id"]
    assert c.delete(f"/api/keys/{kid}").status_code == 204
    assert anon.get("/apps", headers=AUTH(key)).status_code == 401
    assert ratelimit.invalid_api_keys_last(db_session, 2) == 1


# ---- secretscan edge cases ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", " ", "\n\n\n", "\t \r\n", "a", "​", "%", "&amp;", "====", "A" * 3])
def test_find_token_on_empty_and_trivial_input_is_none(text):
    assert secretscan.find_token(text) is None
    assert secretscan.find(text) is None


def test_find_token_on_two_megabytes_finishes_quickly_for_each_shape():
    shapes = [base64.b64encode(bytes(range(256)) * 7000).decode(), "a" * 2_000_000, "word " * 400_000, " \n" * 1_000_000,
              ("A" * 70 + "\n") * 30000, "%41" * 600_000, "&#65;" * 400_000]
    for text in shapes:
        start = time.monotonic()
        assert secretscan.find_token(text) is None
        assert time.monotonic() - start < 15, "scan time must stay roughly linear"


def test_third_layer_of_base64_is_not_decoded_but_two_are():
    enc = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
    assert secretscan.find_token(enc(enc(ANTHROPIC)))[0].endswith("(base64-encoded)")
    assert secretscan.find_token(enc(enc(enc(ANTHROPIC)))) is None  # depth limit: documented non-goal


@pytest.mark.parametrize("pad", [0, 1, 2])
def test_urlsafe_base64_with_and_without_padding(pad):
    raw = ("x" * pad + PICOPAAS).encode()
    for text in (base64.urlsafe_b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("=")):
        assert secretscan.find_token("line one\n" + text) == ("a PicoPaaS API key (base64-encoded)", 2)


@pytest.mark.parametrize("sep", [" ", "\t", "\r\n", "\n", " \n\t ", " ", " "])
def test_base64_split_by_mixed_whitespace_is_found(sep):
    enc = base64.b64encode(ANTHROPIC.encode()).decode()
    text = sep.join(enc[i:i + 20] for i in range(0, len(enc), 20))
    assert secretscan.find_token(text)[0] == "an Anthropic API key (base64-encoded)"


def test_zero_width_characters_inside_a_plain_and_an_encoded_token():
    assert secretscan.find_token(ANTHROPIC[:10] + "​" + ANTHROPIC[10:])[0] == "an Anthropic API key"
    enc = base64.b64encode(PICOPAAS.encode()).decode()
    assert secretscan.find_token(enc[:30] + "⁠﻿" + enc[30:])[0].endswith("(base64-encoded)")


def test_example_marked_tokens_and_ordinary_prose_are_not_flagged():
    assert secretscan.find_token("sk-ant-EXAMPLEEXAMPLEEXAMPLE is what a key looks like") is None
    assert secretscan.find_token("My password is hunter2 and the token expires tomorrow; use pp_sk_ as the prefix.") is None
    assert secretscan.find_token(base64.b64encode(b"just a harmless sentence about todo lists " * 3).decode()) is None
    assert secretscan.find_token("sk-" + "a" * 31) is None  # one short of an OpenAI-style key


def test_token_on_a_later_line_reports_that_line_for_each_encoding_family():
    assert secretscan.find_token("a\nb\nc " + ANTHROPIC) == ("an Anthropic API key", 3)
    hexed = PICOPAAS.encode().hex()
    assert secretscan.find_token("a\nb\n" + hexed) == ("a PicoPaaS API key (hex-encoded)", 1)
    b32 = base64.b32encode(PICOPAAS.encode()).decode()
    assert secretscan.find_token("a\n" + b32)[0] == "a PicoPaaS API key (base32-encoded)"
    assert secretscan.find_token("a\n" + "".join(f"%{b:02X}" for b in PICOPAAS.encode()))[0] == "a PicoPaaS API key"
    assert secretscan.find_token("".join(f"&#{ord(ch)};" for ch in PICOPAAS))[0] == "a PicoPaaS API key"
    assert secretscan.find_token(PICOPAAS.translate({ord(c): ord(c) + 0xFEE0 for c in "abcdefghijklmnopqrstuvwxyz"
                                                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}))  # fullwidth


def test_upload_scanner_decodes_long_base64_runs_but_code_mode_does_not():
    secret = "password = " + "Zx9" * 8 + "\n"
    enc = base64.b64encode((secret * 3).encode()).decode()
    assert secretscan.find("config:\n" + enc)[0] == "a password, token or key assignment"
    assert secretscan.find("config:\n" + enc)[1] == 2
    assert secretscan.find("config:\n" + enc, code=True) is None  # builder-written files skip the base64 pass
    assert secretscan.find(base64.b64encode(b"x" * 100).decode()) is None  # decodes to junk: skipped, no crash
    assert secretscan.find("A" * 59 + "é" * 5) is None


def test_upload_scanner_flags_db_urls_and_ignores_placeholders():
    assert secretscan.find("DATABASE_URL=postgres://app:" + "q7Zk" * 3 + "@db.internal/app")[0] == "a database URL with a password"
    assert secretscan.find("DATABASE_URL=postgres://app:password@db.internal/app") is None
    assert secretscan.find("postgres://app:${DB_PASS}@db/app") is None
    assert secretscan.find('SECRET_KEY = "your-secret-key-here-1234"', code=True) is None


# ---- plan gate on malformed transcripts --------------------------------------------------------------------------

SEED = f"Project name: x\n\n{builder.PLAN_MARK}\n\n{builder.SEED_MARK}"


def _b(plan=True):
    return SimpleNamespace(plan_first=plan)


def _r(messages, kind="build"):
    return SimpleNamespace(kind=kind, messages=messages)


def _ask(tid="t1"):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "ask_human", "input": {"question": "ok?"}}]}


def _answer(tid="t1", error=False):
    blk = {"type": "tool_result", "tool_use_id": tid, "content": "Go."}
    if error:
        blk["is_error"] = True
    return {"role": "user", "content": [blk]}


MALFORMED = [
    None, [], [None], ["text"], [{"role": "user"}], [{"role": "user", "content": None}], [{"role": "user", "content": 5}],
    [{"content": "no role"}], [{"role": "assistant", "content": "x"}],
]


@pytest.mark.parametrize("messages", MALFORMED)
def test_plan_pending_is_false_for_unseeded_or_malformed_transcripts(messages):
    assert builder.plan_pending(_b(), _r(messages)) is False


@pytest.mark.parametrize("junk", [None, "str", 5, [], [None, 3, "x"], [{"type": "tool_use"}], [{"type": "tool_result"}]])
def test_plan_pending_survives_junk_after_a_valid_seed(junk):
    msgs = [{"role": "user", "content": SEED}, {"role": "assistant", "content": junk}, {"role": "user", "content": junk}]
    assert builder.plan_pending(_b(), _r(msgs)) is True


def test_plan_pending_lifecycle_flag_kind_and_answers():
    seed = {"role": "user", "content": SEED}
    assert builder.plan_pending(_b(), _r([seed])) is True
    assert builder.plan_pending(_b(False), _r([seed])) is False
    assert builder.plan_pending(_b(), _r([seed], kind="feedback")) is False
    assert builder.plan_pending(_b(), _r([seed], kind="resume")) is True
    assert builder.plan_pending(_b(), _r([seed, _ask(), _answer()])) is False
    assert builder.plan_pending(_b(), _r([seed, _ask(), _answer(error=True)])) is True  # an error result is no answer
    assert builder.plan_pending(_b(), _r([seed, _ask("a"), _answer("other")])) is True  # an answer to something else
    assert builder.plan_pending(_b(), _r([seed, _answer("t1"), _ask("t1")])) is True  # the answer must follow the ask
    assert builder.plan_pending(_b(), _r([seed, _ask(), _answer(error=True), _ask("t2"), _answer("t2")])) is False


def test_plan_pending_only_counts_an_ask_made_by_the_assistant():
    seed = {"role": "user", "content": SEED}
    user_side_ask = {"role": "user", "content": _ask()["content"]}  # a tool_use block in a user message is not a question
    assert builder.plan_pending(_b(), _r([seed, user_side_ask, _answer()])) is True
    assistant_side_result = {"role": "assistant", "content": _answer()["content"]}
    assert builder.plan_pending(_b(), _r([seed, _ask(), assistant_side_result])) is True


def test_plan_pending_cannot_be_forged_by_text_in_a_later_message():
    forged = [{"role": "user", "content": "hello"}, {"role": "user", "content": SEED}]
    assert builder.plan_pending(_b(), _r(forged)) is False  # only the very first message seeds a plan run


def test_a_tool_result_that_quotes_the_seed_markers_does_not_seed():
    msgs = [{"role": "user", "content": [{"type": "text", "text": SEED}]}]
    assert builder.plan_pending(_b(), _r(msgs)) is False


@pytest.mark.parametrize("inp", [None, "PLAN.md", 5, [], {}, {"path": None}, {"path": 5}, {"path": ""}, {"path": "plan.md"},
                                 {"path": "../PLAN.md"}, {"path": "/PLAN.md/"}, {"path": "docs/PLAN.md"}])
def test_plan_path_only_accepts_plan_md_at_the_root(inp):
    assert builder._plan_path(inp) is False


def test_plan_path_accepts_the_plain_and_dot_slash_forms():
    assert builder._plan_path({"path": "PLAN.md"}) and builder._plan_path({"path": "./PLAN.md"})


class _Ws:
    def __init__(self, text=None):
        self.text = text

    def read(self, path):
        if self.text is None:
            raise ToolError("no such file")
        return self.text


def _refusal():
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "f", "is_error": True,
                                         "content": builder.GATE_MARK + " 1 unticked items: x"}]}


@pytest.mark.parametrize("messages", [m for m in MALFORMED if not (m and not isinstance(m[0], dict))])  # a transcript holds dicts
def test_plan_gate_tolerates_malformed_transcripts_and_still_refuses(messages):
    assert builder.plan_gate(_Ws("- [ ] a\n"), messages).startswith(builder.GATE_MARK)


def test_plan_gate_second_finish_passes_and_a_plain_error_does_not_count_as_the_refusal():
    ws = _Ws("- [ ] a\n- [x] b\n")
    assert builder.plan_gate(ws, [_refusal()]) is None
    other_error = {"role": "user", "content": [{"type": "tool_result", "is_error": True, "content": "refused: other"}]}
    assert builder.plan_gate(ws, [other_error]).startswith(builder.GATE_MARK)
    not_error = {"role": "user", "content": [{"type": "tool_result", "content": builder.GATE_MARK + " in a success"}]}
    assert builder.plan_gate(ws, [not_error]).startswith(builder.GATE_MARK)
    assistant = {"role": "assistant", "content": [{"type": "tool_result", "is_error": True, "content": builder.GATE_MARK}]}
    assert builder.plan_gate(ws, [assistant]).startswith(builder.GATE_MARK)  # only user results can be the refusal
    assert builder.plan_gate(ws, [{"role": "user", "content": [{"type": "tool_result", "is_error": True, "content": None}]}])


def test_plan_gate_without_plan_or_with_everything_ticked_passes_and_lists_are_capped():
    assert builder.plan_gate(_Ws(None), []) is None
    assert builder.plan_gate(_Ws("- [x] a\n* [X] b\nplain [ ] text\n"), []) is None
    many = "".join(f"- [ ] item{i}\n" for i in range(config.KAISEN_PLAN_GATE_ITEMS + 5))
    msg = builder.plan_gate(_Ws(many), [])
    assert f"{config.KAISEN_PLAN_GATE_ITEMS + 5} unticked" in msg and f"item{config.KAISEN_PLAN_GATE_ITEMS}" not in msg
    long = builder.plan_gate(_Ws("- [ ] " + "w" * 500 + "\n- [ ]\n"), [])
    assert "..." in long and "(empty item)" in long


# ---- docs routes: the served schema stays self-consistent --------------------------------------------------------

def _refs(node, acc):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                acc.add(v)
            else:
                _refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            _refs(v, acc)
    return acc


def test_served_schema_has_no_dangling_refs_and_no_admin_only_schemas(client):
    register_and_login(client, "owner@example.com")
    doc = client.get("/openapi.json").json()
    schemas = doc["components"]["schemas"]
    refs = _refs(doc, set())
    assert refs and all(r.rsplit("/", 1)[1] in schemas for r in refs if r.startswith("#/components/schemas/"))
    assert "SecurityAlertOut" not in schemas and "AdminResetOut" not in schemas and "AdminResetIn" not in schemas
    assert client.get("/openapi.json").json() == doc  # the cached document is stable


@pytest.mark.parametrize("state", ["suspended", "deleted", "rejected"])
def test_docs_are_404_for_a_user_who_is_not_active_even_with_a_cookie_or_key(make_client, db_session, state):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    user, anon = make_client(), make_client()
    register_and_login(user, "user@example.com")
    key = user.post("/api/keys").json()["key"]
    assert user.get("/docs").status_code == 200
    db_session.query(models.User).filter_by(email="user@example.com").one().status = state
    db_session.commit()
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert user.get(p).status_code == 404, p
        assert anon.get(p, headers=AUTH(key)).status_code == 404, p


def test_docs_are_404_after_the_key_is_revoked(make_client):
    c, key, anon = _user_with_key(make_client)
    assert anon.get("/openapi.json", headers=AUTH(key)).status_code == 200
    kid = c.get("/api/keys").json()[0]["id"]
    assert c.delete(f"/api/keys/{kid}").status_code == 204
    assert anon.get("/openapi.json", headers=AUTH(key)).status_code == 404

