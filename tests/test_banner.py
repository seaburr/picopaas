from app import config, models
from tests.conftest import register_and_login

GOOD = {"enabled": True, "level": "info", "title": "Maintenance", "text": "Back at noon."}


def _admin(make_client, email="admin@example.com"):
    c = make_client()
    register_and_login(c, email)  # first user is the platform admin
    return c


def test_defaults_public_no_auth(client):
    r = client.get("/api/banner")
    assert r.status_code == 200
    assert r.json() == {"enabled": config.BANNER_ENABLED, "level": config.BANNER_LEVEL,
                        "title": config.BANNER_TITLE, "text": config.BANNER_TEXT}
    assert r.json()["enabled"] is True and r.json()["level"] == "warning"
    assert "don't store personal data" in r.json()["text"]


def test_admin_get_defaults(make_client):
    admin = _admin(make_client)
    assert admin.get("/admin/banner").json()["title"] == config.BANNER_TITLE


def test_put_happy_path_persists_and_audits(make_client, db_session):
    admin = _admin(make_client)
    r = admin.put("/admin/banner", json={**GOOD, "title": "  Maintenance  "})
    assert r.status_code == 200 and r.json() == GOOD
    assert admin.get("/api/banner").json() == GOOD
    assert admin.get("/admin/banner").json() == GOOD
    db_session.expire_all()
    assert db_session.get(models.PlatformSetting, "banner_text").value == "Back at noon."
    log = db_session.query(models.AuditLog).filter_by(action="banner.update").all()
    assert len(log) == 1 and "enabled True -> True" in log[0].detail and "level warning -> info" in log[0].detail
    # a second save updates rows in place
    assert admin.put("/admin/banner", json={**GOOD, "enabled": False, "text": ""}).status_code == 200
    assert admin.get("/api/banner").json()["enabled"] is False
    assert db_session.query(models.PlatformSetting).filter(models.PlatformSetting.key.like("banner_%")).count() == 4


def test_public_get_works_anonymous_after_save(make_client):
    admin = _admin(make_client)
    admin.put("/admin/banner", json=GOOD)
    anon = make_client()
    assert anon.get("/api/banner").json() == GOOD


def test_non_admin_403(make_client):
    _admin(make_client)
    user = make_client()
    register_and_login(user, "user@example.com")
    assert user.put("/admin/banner", json=GOOD).status_code == 403
    assert user.get("/admin/banner").status_code == 403
    assert user.get("/api/banner").json()["text"] == config.BANNER_TEXT  # unchanged


def test_anonymous_401(client):
    assert client.put("/admin/banner", json=GOOD).status_code == 401
    assert client.get("/admin/banner").status_code == 401


def test_validation(make_client, db_session):
    admin = _admin(make_client)
    bad = [
        {**GOOD, "title": "x" * (config.BANNER_TITLE_MAX + 1)},
        {**GOOD, "text": "x" * (config.BANNER_TEXT_MAX + 1)},
        {**GOOD, "level": "error"},
        {**GOOD, "text": "line1\nline2"},
        {**GOOD, "title": "tab\there"},
        {**GOOD, "text": "bell\x07"},
        {**GOOD, "text": "   "},
        {**GOOD, "extra": 1},
        {"enabled": True, "level": "info", "title": "t"},
    ]
    for body in bad:
        assert admin.put("/admin/banner", json=body).status_code == 422, body
    assert db_session.query(models.PlatformSetting).filter(models.PlatformSetting.key.like("banner_%")).count() == 0
    max_ok = {**GOOD, "title": "x" * config.BANNER_TITLE_MAX, "text": "y" * config.BANNER_TEXT_MAX}
    assert admin.put("/admin/banner", json=max_ok).status_code == 200


def test_plain_text_is_stored_verbatim(make_client):
    admin = _admin(make_client)
    body = {**GOOD, "text": "<b>hi</b> & bye"}
    assert admin.put("/admin/banner", json=body).json()["text"] == "<b>hi</b> & bye"


def _rows(db_session):
    return {r.key: r.value for r in db_session.query(models.PlatformSetting).filter(models.PlatformSetting.key.like("banner_%"))}


def test_second_client_sees_put_immediately_despite_cache(make_client):
    admin = _admin(make_client)
    reader = make_client()
    assert reader.get("/api/banner").json()["text"] == config.BANNER_TEXT  # primes the settings cache
    assert admin.put("/admin/banner", json=GOOD).status_code == 200
    assert reader.get("/api/banner").json() == GOOD  # cache invalidated by the PUT, no TTL wait
    assert reader.get("/api/banner").json() == GOOD  # and a cache hit returns the same


def test_partial_stored_rows_fall_back_to_defaults(make_client, db_session):
    from app import platform_settings
    admin = _admin(make_client)
    db_session.add(models.PlatformSetting(key="banner_title", value="Only title", updated_by="x"))
    db_session.commit()
    platform_settings.invalidate()
    assert admin.get("/api/banner").json() == {"enabled": config.BANNER_ENABLED, "level": config.BANNER_LEVEL,
                                               "title": "Only title", "text": config.BANNER_TEXT}


def test_stored_enabled_zero_and_bad_level(make_client, db_session):
    from app import platform_settings
    admin = _admin(make_client)
    for k, v in {"banner_enabled": "0", "banner_level": "error"}.items():
        db_session.add(models.PlatformSetting(key=k, value=v, updated_by="x"))
    db_session.commit()
    platform_settings.invalidate()
    out = admin.get("/api/banner").json()
    assert out["enabled"] is False and out["level"] == config.BANNER_LEVEL


def test_put_after_partial_rows_updates_missing_and_existing(make_client, db_session):
    admin = _admin(make_client)
    db_session.add(models.PlatformSetting(key="banner_title", value="old", updated_by="x"))
    db_session.commit()
    assert admin.put("/admin/banner", json=GOOD).status_code == 200
    db_session.expire_all()
    assert _rows(db_session) == {"banner_enabled": "1", "banner_level": "info",
                                 "banner_title": "Maintenance", "banner_text": "Back at noon."}


def test_invisible_and_separator_characters_rejected(make_client, db_session):
    admin = _admin(make_client)
    for ch in ("‮", "​", " ", " ", "﻿", "\x7f", "\x85", ""):
        assert admin.put("/admin/banner", json={**GOOD, "text": f"a{ch}b"}).status_code == 422, repr(ch)
        assert admin.put("/admin/banner", json={**GOOD, "title": f"a{ch}b"}).status_code == 422, repr(ch)
    assert _rows(db_session) == {}
    assert admin.put("/admin/banner", json={**GOOD, "text": "café — ok"}).status_code == 200


def test_disabled_with_empty_text_allowed_and_audit_unchanged(make_client, db_session):
    admin = _admin(make_client)
    admin.put("/admin/banner", json=GOOD)
    admin.put("/admin/banner", json=GOOD)
    last = db_session.query(models.AuditLog).filter_by(action="banner.update").order_by(models.AuditLog.id.desc()).first()
    assert "title and text unchanged" in last.detail


def test_api_keys_on_banner_routes(make_client):
    admin = _admin(make_client)
    user = make_client()
    register_and_login(user, "keyuser@example.com")
    hdr = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731
    ukey = user.post("/api/keys").json()["key"]
    akey = admin.post("/api/keys").json()["key"]
    other = make_client()
    assert other.put("/admin/banner", json=GOOD, headers=hdr(ukey)).status_code == 403
    assert other.get("/api/banner", headers=hdr(ukey)).status_code == 200
    # the banner is not a session-only route: an admin key is accepted (documents current behaviour)
    assert other.put("/admin/banner", json=GOOD, headers=hdr(akey)).status_code == 200
