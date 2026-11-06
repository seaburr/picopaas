"""Owner resource changes are rate limited per app (each rolls the pod; storage changes can swap the bucket key)."""
from app import config
from tests.test_owner_resources import APP, _admin, _patch, env  # noqa: F401  (fixture)


def test_owner_changes_limited_per_hour_but_noops_and_admin_changes_do_not_count(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "OWNER_RESOURCE_CHANGES_PER_HOUR", 3)
    aid = env.owner.post("/apps", json=APP).json()["id"]
    for v in (60, 70, 80):
        assert _patch(env, aid, {"cpu_limit_m": v}).status_code == 200
    assert _patch(env, aid, {"cpu_limit_m": 80}).status_code == 200  # same value: not a change
    r = _patch(env, aid, {"cpu_limit_m": 90})
    assert r.status_code == 429 and r.headers["Retry-After"]
    _admin(env, aid, {"db_quota_mb": 40})  # an admin change neither counts nor is blocked
    assert _patch(env, aid, {"cpu_limit_m": 90}).status_code == 429
