"""Coverage pass 2026-11-13: guard rails and error branches of the Kaisen router/builder/settings/uploads and the advisor.
Real register/login flow; only infra and the model client are faked."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import advisor, config, models
from app.kaisen import builder, settings as ksettings, uploads as kuploads, watchdog
from app.kaisen.llm import FakeModelClient, reply
from app.main import app as fastapi_app
from app.routers.kaisen import get_model_factory
from app.kaisen.models import KaisenBuild, KaisenMemberLimit, KaisenRun, KaisenSnapshot
from tests.conftest import register_and_login
from tests.kaisen_helpers import db
from tests.test_advisor import NOW, facts, run
from tests.test_kaisen_api import _add_files, _new_build, fakes, team  # noqa: F401 (fixtures)
from tests.test_kaisen_stop import _wait

USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


@pytest.fixture()
def model(team):
    """The improve-brief route wants a scripted FakeModelClient; the team fixture's fake only serves builder runs."""
    fake = FakeModelClient()
    fastapi_app.dependency_overrides[get_model_factory] = lambda: (lambda key: fake)
    return fake


def _uid(email):
    return db().query(models.User).filter_by(email=email).one().id


# ---- router -----------------------------------------------------------------------------------------

def test_test_anthropic_without_a_key_says_so(make_client, fakes):  # noqa: F811
    make_client()  # first user = admin
    c = make_client()
    register_and_login(c, "solo@example.com")
    pid = c.post("/projects", json={"name": "NoKey"}).json()["id"]
    r = c.post(f"/projects/{pid}/kaisen/test/anthropic")
    assert r.status_code == 200 and r.json() == {"ok": False, "message": "No Anthropic API key is saved for this project yet."}


def test_improve_brief_defaults_to_the_personal_project_and_respects_its_suspension(team, model):  # noqa: F811
    admin, a, _, _, _ = team
    personal = next(p for p in a.get("/projects").json() if p["is_personal"])["id"]
    assert a.put(f"/projects/{personal}/kaisen", json={"anthropic_key": "not-the-anthropic-key"}).status_code == 200
    model.script = [reply({"type": "text", "text": "A sharper brief."}, usage=USAGE)]
    r = a.post("/kaisen/improve-brief", json={"brief": "A todo list"})  # no project_id
    assert r.status_code == 200 and r.json()["brief"] == "A sharper brief."
    assert admin.post(f"/admin/projects/{personal}/suspend", json={"reason": "testing"}).status_code in (200, 204)
    r = a.post("/kaisen/improve-brief", json={"brief": "A todo list"})
    assert r.status_code == 403 and len(model.calls) == 1  # refused before any model call


@pytest.mark.parametrize("model_text,detail", [("", "The model returned no text. Try again."),
                                               ("Use sk-ant-api03-" + "Ab1" * 12, "The suggestion was discarded. Try again.")])
def test_improve_brief_discards_an_empty_or_secret_suggestion(team, model, model_text, detail):  # noqa: F811
    _, a, _, _, pid = team
    model.script = [reply({"type": "text", "text": model_text}, usage=USAGE)]
    r = a.post("/kaisen/improve-brief", json={"project_id": pid, "brief": "A todo list"})
    assert r.status_code == 502 and r.json()["detail"] == detail
    # the call was made and billed even though the text is thrown away: audited, but nothing is attached to a build
    assert len(model.calls) == 1


def test_delete_build_with_admin_suspended_app_is_refused_only_when_deleting_the_app(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    app_id = a.post(f"/kaisen/builds/{bid}/deploy").json()["app"]["id"]
    s = db()
    s.get(models.App, app_id).status = "suspended"
    s.commit()
    r = a.delete(f"/kaisen/builds/{bid}", params={"delete_app": True})
    assert r.status_code == 403 and "suspended by an administrator" in r.json()["detail"]
    assert a.get(f"/kaisen/builds/{bid}").status_code == 200  # nothing was deleted
    assert a.delete(f"/kaisen/builds/{bid}").status_code == 204  # keeping the app is fine


def test_interrupt_lost_race_is_409(team, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    s = db()
    r = s.query(KaisenRun).filter_by(build_id=bid).one()
    r.status = "running"
    s.commit()
    monkeypatch.setattr(watchdog, "interrupt", lambda *args, **kw: "stale")  # the step finished between our check and the CAS
    resp = a.post(f"/kaisen/builds/{bid}/runs/{r.id}/interrupt")
    assert resp.status_code == 409 and "no step in progress" in resp.json()["detail"]


@pytest.mark.parametrize("path,body", [("feedback", {"text": "   "}), ("fork", {"name": "   "})])
def test_blank_text_is_422(team, path, body):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    s = db()
    for r in s.query(KaisenRun).filter_by(build_id=bid):
        r.status = "done"
    s.get(KaisenBuild, bid).status = "done"
    s.commit()
    assert a.post(f"/kaisen/builds/{bid}/{path}", json=body).status_code == 422


def test_feedback_is_refused_when_the_member_daily_limit_is_reached(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    s = db()
    for r in s.query(KaisenRun).filter_by(build_id=bid):
        r.status = "done"
    s.get(KaisenBuild, bid).status = "done"
    s.add(KaisenMemberLimit(project_id=pid, user_id=_uid("a@example.com"), cap_day=0.0, cap_build=None))
    s.commit()
    r = a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "more blue"})
    assert r.status_code == 409 and "member daily limit" in r.json()["detail"]
    assert db().query(KaisenRun).filter_by(build_id=bid).count() == 1  # no run queued


def test_restore_succeeds_even_when_the_missing_uploads_scan_fails(team, monkeypatch):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    s = db()
    for r in s.query(KaisenRun).filter_by(build_id=bid):
        r.status = "done"
    s.get(KaisenBuild, bid).status = "done"
    s.commit()
    assert a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "change"}).status_code == 200
    sid = db().query(KaisenSnapshot).filter_by(build_id=bid).one().id
    for r in db().query(KaisenRun).filter_by(build_id=bid):
        r.status = "done"
    db().get(KaisenBuild, bid).status = "done"
    db().commit()
    monkeypatch.setattr(kuploads, "project_enabled", lambda *a_, **k: True)
    monkeypatch.setattr(kuploads, "platform_enabled", lambda *a_, **k: True)

    def boom(*a_, **k):
        raise RuntimeError("scan broke")
    monkeypatch.setattr(kuploads, "missing_from", boom)
    r = a.post(f"/kaisen/builds/{bid}/snapshots/{sid}/restore")
    assert r.status_code == 200 and r.json()["missing_uploads"] == []


def test_download_of_a_build_without_files_is_409(team):
    _, a, _, _, pid = team
    bid = a.post("/kaisen/builds", json={"project_id": pid, "name": "Empty", "brief": "x"}).json()["id"]
    r = a.get(f"/kaisen/builds/{bid}/download")
    assert r.status_code == 409 and r.json()["detail"] == "The build has no files yet."


def test_output_test_reports_a_project_token_that_was_removed_after_saving(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    url = f"/kaisen/builds/{bid}/output/test"
    assert "download only" in a.post(url).json()["message"]
    assert a.put(f"/kaisen/builds/{bid}/output", json={"mode": "github"}).status_code == 422  # saving needs repo and token
    assert a.put(f"/projects/{pid}/kaisen", json={"github_token": "not-the-github-token"}).status_code == 200
    assert a.put(f"/kaisen/builds/{bid}/output", json={"mode": "github", "repo": "https://github.com/example/demo"}).status_code == 200
    assert a.put(f"/projects/{pid}/kaisen", json={"clear_github_token": True}).status_code == 200  # an owner removes it later
    r = a.post(url).json()
    assert r["ok"] is False and r["message"].startswith("No access token is available")


# ---- builder ----------------------------------------------------------------------------------------

def test_stop_waiting_loses_to_a_run_that_is_no_longer_waiting(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    rid, _ = _wait(bid)
    s = db()
    stale = s.get(KaisenRun, rid)  # loaded while waiting
    s.query(KaisenRun).filter_by(id=rid).update({"status": "running"})  # an answer won the race
    s.commit()
    assert builder.stop_waiting(s, stale, "a@example.com") is False
    s.expire_all()
    assert s.get(KaisenRun, rid).status == "running" and s.get(KaisenRun, rid).error is None


def test_stop_reason_is_none_when_the_build_is_gone(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    s = db()
    run_ = s.query(KaisenRun).filter_by(build_id=bid).one()
    assert builder.stop_reason(s, run_) is None  # active project, active member
    run_.build_id = 987654  # the build vanished under the run
    assert builder.stop_reason(s, run_) is None
    s.rollback()


# ---- settings and uploads ---------------------------------------------------------------------------

def test_parse_domains_accepts_text_dedupes_and_rejects_bad_input():
    assert ksettings.parse_domains("Docs.Example.com, docs.example.com/\n  api.example.org/v1  ,,") == ["docs.example.com", "api.example.org/v1"]
    assert ksettings.parse_domains(["a.example.com", " ", "a.example.com"]) == ["a.example.com"]
    with pytest.raises(ValueError, match="must be a list"):
        ksettings.parse_domains(5)
    with pytest.raises(ValueError, match="not a domain"):
        ksettings.parse_domains(["https://a.example.com"])
    with pytest.raises(ValueError, match="not a domain"):
        ksettings.parse_domains(["localhost"])
    limit = config.KAISEN_WEB_DOMAINS_MAX
    assert len(ksettings.parse_domains([f"d{i}.example.com" for i in range(limit)])) == limit
    with pytest.raises(ValueError, match=f"At most {limit}"):
        ksettings.parse_domains([f"d{i}.example.com" for i in range(limit + 1)])


def test_settings_reject_bad_values_and_clear_the_github_token(team):
    _, a, _, _, pid = team
    url = f"/projects/{pid}/kaisen"
    assert a.put(url, json={"package_steward": "maybe"}).status_code == 422
    assert a.put(url, json={"github_token": "not-the-github-token"}).json()["github_token"]["set"] is True
    assert a.put(url, json={"clear_github_token": True}).json()["github_token"]["set"] is False


def test_missing_from_ignores_upload_files_and_files_without_references(db_session):
    s = db_session
    assert kuploads.missing_from(s, 1, {"uploads/logo.png": "uploads/other.png", "index.html": "<h1>no refs</h1>"}) == []
    assert kuploads.missing_from(s, 1, {}) == []


# ---- advisor ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,text", [(90, "2 min"), (30, "1 min"), (3600, "1 h"), (5400, "1.5 h"), (86400, "24 h"), (172800, "2 d")])
def test_duration_wording(seconds, text):
    assert advisor._dur(seconds) == text


def test_quota_severity_at_exact_thresholds():
    sev = advisor._quota_severity
    assert sev(10, 0, False, 80, 95) is None  # no quota: never a finding
    assert sev(79, 100, False, 80, 95) is None and sev(80, 100, False, 80, 95) == "warning"
    assert sev(94, 100, False, 80, 95) == "warning" and sev(95, 100, False, 80, 95) == "critical"
    assert sev(1, 100, True, 80, 95) == "critical"


def test_unused_addon_lists_database_and_bucket_at_the_exact_cutoff():
    days = config.ADVISOR_ADDON_UNUSED_DAYS
    edge = NOW - timedelta(days=days)
    x = run("addon.unused", facts(db_size_bytes=0, db_created_at=edge, bucket_size_bytes=0, bucket_created_at=edge))
    assert x.title == "Database and Bucket unused" and x.severity == "info"
    assert run("addon.unused", facts(db_size_bytes=0, db_created_at=edge + timedelta(seconds=1))) is None  # one second too young


def test_malformed_and_legacy_metric_rows_never_break_the_loader():
    ts = NOW
    bad = advisor._sample_fact(models.AppMetricSample(app_id=1, ts=ts, cpu_m=10, mem_mi=300, pods=3, per_pod="{not json"))
    assert (bad.cpu_m, bad.mem_mi) == (4, 100)  # falls back to the mean per pod, rounded up
    legacy = advisor._sample_fact(models.AppMetricSample(app_id=1, ts=ts, cpu_m=10, mem_mi=300, pods=0, per_pod=None))
    assert (legacy.cpu_m, legacy.mem_mi) == (10, 300)
    wrong_shape = advisor._sample_fact(models.AppMetricSample(app_id=1, ts=ts, cpu_m=10, mem_mi=300, pods=1, per_pod=json.dumps({"p": 5})))
    assert wrong_shape.mem_mi == 300


def test_loader_reports_failed_backups_per_kind_in_the_advice_response(make_client, db_session):
    admin = make_client()
    register_and_login(admin, "admin@example.com")
    alice = make_client()
    register_and_login(alice, "alice@example.com")
    aid = alice.post("/apps", json={"name": "demo", "image": "ghcr.io/example/demo:1.0", "port": 8080, "env": []}).json()["id"]
    a = db_session.get(models.App, aid)
    a.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    a.status = "running"
    db_session.add(models.AppBackupConfig(app_id=aid, enabled=True, schedule="daily", db_last_status="failed", db_last_error="dump timed out",
                                          vol_last_status="failed", vol_last_error="volume busy", last_status="succeeded"))
    db_session.commit()
    f = next(f for f in alice.get(f"/apps/{aid}/advice").json()["findings"] if f["rule"] == "backup.failing")
    assert "Database backup failed: dump timed out" in f["evidence"]
    assert any(x["rule"] == "backup.failing" and x["app_id"] == aid for x in admin.get("/admin/advice").json()["findings"])


# ---- boundaries -------------------------------------------------------------------------------------

def test_exactly_the_maximum_number_of_improve_ids_is_accepted_and_a_press_just_inside_24h_attaches(team, model):
    from app.kaisen.models import KaisenEvent
    _, a, _, _, pid = team
    model.script = [reply({"type": "text", "text": "better"}, usage=USAGE)]
    ev = a.post("/kaisen/improve-brief", json={"project_id": pid, "brief": "A todo list"}).json()["improve_id"]
    s = db()
    s.get(KaisenEvent, ev).at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=config.KAISEN_IMPROVE_ATTACH_HOURS, minutes=-1)
    s.commit()
    ids = [ev] + list(range(900_001, 900_000 + config.KAISEN_IMPROVE_IDS_MAX))  # exactly the maximum, the rest unknown
    assert len(ids) == config.KAISEN_IMPROVE_IDS_MAX
    r = a.post("/kaisen/builds", json={"project_id": pid, "name": "x", "brief": "y", "improve_ids": ids})
    assert r.status_code == 201 and r.json()["spend"]["build_usd"] > 0
    s.expire_all()
    assert s.get(KaisenEvent, ev).build_id == r.json()["id"]
