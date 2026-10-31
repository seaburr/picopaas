"""Kaisen feedback wave 2 (chunk B2): per-member spend limits, build secrets, postgres/bucket/volume previews."""
import pytest

from app import config, crypto, models
from app.kaisen import builder, deploy as deploy_mod, llm
from app.kaisen.llm import FakeModelClient
from app.kaisen.models import KaisenBuild, KaisenEvent, KaisenSecret
from tests.kaisen_helpers import app_env, db
from tests.test_kaisen_api import _add_files, _audit, _deploy, _new_build, _user, fakes, team  # noqa: F401 (fixtures)
from tests.test_kaisen_worker import PER_CALL, _writes, go

SECRET_VALUE = "not-the-api-token-1"


def _idle(bid):
    """_new_build auto-starts a first run (the project has a key): finish it so a member can start the run under test."""
    s = db()
    run = builder.latest_run(s, bid)
    if run is not None and run.status in builder.ACTIVE:
        builder.set_status(s, run, "done")
        s.commit()


def _uid(email):
    return db().query(models.User).filter_by(email=email).one().id


# ---- F8d: per-member spend limits ----------------------------------------------------------------

def test_member_limits_anonymous_401(client):
    assert client.get("/projects/1/kaisen/members").status_code == 401
    assert client.put("/projects/1/kaisen/members/1", json={"cap_day": 1}).status_code == 401


def test_owner_sets_member_limits_and_audited(team):
    _, a, b, c, pid = team
    r = a.put(f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}", json={"cap_day": 1.5, "cap_build": 0.5})
    assert r.status_code == 200, r.text
    assert r.json()["cap_day"] == 1.5 and r.json()["cap_build"] == 0.5 and r.json()["spend_today_usd"] == 0
    from app.kaisen.models import KaisenMemberLimit
    row = db().get(KaisenMemberLimit, (pid, _uid("b@example.com")))
    assert (row.cap_day, row.cap_build, row.updated_by) == (1.5, 0.5, _uid("a@example.com"))
    assert _audit("kaisen.member_limits_update")[-1].detail.startswith(f"member={_uid('b@example.com')}")
    # null clears one limit, omitted keeps the other
    r = a.put(f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}", json={"cap_day": None})
    assert r.json()["cap_day"] is None and r.json()["cap_build"] == 0.5


def test_member_cannot_edit_limits_and_non_member_gets_404(team):
    _, a, b, c, pid = team
    path = f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}"
    assert b.put(path, json={"cap_day": 100}).status_code == 403
    assert c.put(path, json={"cap_day": 100}).status_code == 404
    assert c.get(f"/projects/{pid}/kaisen/members").status_code == 404
    assert db().query(models.AuditLog).filter_by(action="kaisen.member_limits_update").count() == 0


def test_limits_for_a_non_member_target_404_and_validation_422(team):
    _, a, b, c, pid = team
    assert a.put(f"/projects/{pid}/kaisen/members/{_uid('c@example.com')}", json={"cap_day": 1}).status_code == 404
    path = f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}"
    assert a.put(path, json={"cap_day": -1}).status_code == 422
    assert a.put(path, json={"cap_build": config.KAISEN_CAP_MAX + 1}).status_code == 422
    assert a.put(path, json={"cap_day": "lots"}).status_code == 422
    assert a.put(path, json={"bogus": 1}).status_code == 422


def test_member_reads_only_own_row_owner_reads_all(team):
    _, a, b, c, pid = team
    a.put(f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}", json={"cap_day": 2})
    mine = b.get(f"/projects/{pid}/kaisen/members").json()
    assert mine["can_edit"] is False and [m["email"] for m in mine["members"]] == ["b@example.com"]
    assert mine["members"][0]["cap_day"] == 2
    owner_view = a.get(f"/projects/{pid}/kaisen/members").json()
    assert owner_view["can_edit"] is True and {m["email"] for m in owner_view["members"]} == {"a@example.com", "b@example.com"}


def test_run_start_refused_when_member_daily_limit_reached(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    _idle(bid)
    a.put(f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}", json={"cap_day": 0})
    r = b.post(f"/kaisen/builds/{bid}/run")
    assert r.status_code == 409 and "member daily limit of $0.00" in r.json()["detail"]
    assert db().query(builder.KaisenRun).filter_by(build_id=bid).count() == 1  # nothing queued for b
    assert a.post(f"/kaisen/builds/{bid}/run").status_code == 200  # the owner has no limit


def test_create_build_reports_member_limit_as_start_error(team):
    _, a, b, c, pid = team
    a.put(f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}", json={"cap_day": 0})
    r = b.post("/kaisen/builds", json={"project_id": pid, "name": "Mine", "brief": "x"})
    assert r.status_code == 201 and "member daily limit" in r.json()["start_error"] and r.json()["status"] == "idle"


def test_mid_run_member_cap_stops_run_and_counts_spend(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    a.put(f"/projects/{pid}/kaisen", json={"cap_run": 50, "cap_day": 50, "cap_build": 50})
    a.put(f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}", json={"cap_day": PER_CALL * 1.5})
    _idle(bid)
    assert b.post(f"/kaisen/builds/{bid}/run").status_code == 200
    model = FakeModelClient(_writes(5))
    go(model)
    assert len(model.calls) == 2  # the second call crossed the member cap, the third was refused
    d = a.get(f"/kaisen/builds/{bid}").json()
    assert d["status"] == "stopped" and "member daily limit" in d["last_error"]
    events = db().query(KaisenEvent).filter_by(build_id=bid).all()
    assert events and all(e.user_id == _uid("b@example.com") for e in events)
    mine = b.get(f"/projects/{pid}/kaisen/members").json()["members"][0]
    assert mine["spend_today_usd"] == pytest.approx(PER_CALL * 2, rel=1e-3)
    owner = {m["email"]: m for m in a.get(f"/projects/{pid}/kaisen/members").json()["members"]}
    assert owner["a@example.com"]["spend_today_usd"] == 0


def test_per_build_member_cap(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    _idle(bid)
    a.put(f"/projects/{pid}/kaisen/members/{_uid('b@example.com')}", json={"cap_build": 0})
    r = b.post(f"/kaisen/builds/{bid}/run")
    assert r.status_code == 409 and "per-build limit" in r.json()["detail"]


# ---- F8c: build secrets --------------------------------------------------------------------------

def test_secrets_anonymous_401(client):
    assert client.get("/kaisen/builds/1/secrets").status_code == 401
    assert client.put("/kaisen/builds/1/secrets/MY_KEY", json={"value": SECRET_VALUE}).status_code == 401
    assert client.delete("/kaisen/builds/1/secrets/MY_KEY").status_code == 401


def test_member_sets_secret_encrypted_masked_and_audited(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    r = b.put(f"/kaisen/builds/{bid}/secrets/WEATHER_API_KEY", json={"value": SECRET_VALUE})
    assert r.status_code == 200, r.text
    assert SECRET_VALUE not in r.text and r.json()["secrets"][0]["name"] == "WEATHER_API_KEY"
    for resp in (a.get(f"/kaisen/builds/{bid}/secrets"), a.get(f"/kaisen/builds/{bid}")):
        assert SECRET_VALUE not in resp.text
    assert a.get(f"/kaisen/builds/{bid}/secrets").json()[0]["value"] == "********"
    row = db().query(KaisenSecret).one()
    assert SECRET_VALUE not in row.value_enc and crypto.decrypt(row.value_enc) == SECRET_VALUE
    assert _audit("kaisen.secret_set")[-1].detail.startswith("name=WEATHER_API_KEY")
    assert SECRET_VALUE not in _audit("kaisen.secret_set")[-1].detail
    # replace keeps one row
    assert b.put(f"/kaisen/builds/{bid}/secrets/WEATHER_API_KEY", json={"value": SECRET_VALUE + "2"}).status_code == 200
    assert db().query(KaisenSecret).count() == 1


def test_secrets_non_member_404(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    assert c.get(f"/kaisen/builds/{bid}/secrets").status_code == 404
    assert c.put(f"/kaisen/builds/{bid}/secrets/MY_KEY", json={"value": SECRET_VALUE}).status_code == 404
    assert c.delete(f"/kaisen/builds/{bid}/secrets/MY_KEY").status_code == 404
    assert db().query(KaisenSecret).count() == 0


@pytest.mark.parametrize("name,value", [
    ("lower_case", SECRET_VALUE), ("1BAD", SECRET_VALUE), ("KAISEN_X", SECRET_VALUE), ("DB_HOST", SECRET_VALUE),
    ("DATABASE_URL", SECRET_VALUE), ("S3_BUCKET", SECRET_VALUE), ("PORT", SECRET_VALUE), ("NODE_OPTIONS", SECRET_VALUE),
    ("A" * 65, SECRET_VALUE), ("MY_KEY", "short"), ("MY_KEY", "x" * (config.KAISEN_SECRET_VALUE_MAX + 1)),
    ("MY_KEY", "${{ db.url }}"),
])
def test_secret_validation_422(team, name, value):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    assert a.put(f"/kaisen/builds/{bid}/secrets/{name}", json={"value": value}).status_code == 422
    assert db().query(KaisenSecret).count() == 0


def test_secret_count_limit(team, monkeypatch):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    monkeypatch.setattr(config, "KAISEN_SECRETS_MAX", 2)
    for n in ("ONE_KEY", "TWO_KEY"):
        assert a.put(f"/kaisen/builds/{bid}/secrets/{n}", json={"value": SECRET_VALUE}).status_code == 200
    assert a.put(f"/kaisen/builds/{bid}/secrets/THREE_KEY", json={"value": SECRET_VALUE}).status_code == 422
    assert a.put(f"/kaisen/builds/{bid}/secrets/ONE_KEY", json={"value": SECRET_VALUE + "x"}).status_code == 200


def test_secret_reaches_app_env_on_every_deploy_and_survives_redeploy(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    b.put(f"/kaisen/builds/{bid}/secrets/WEATHER_API_KEY", json={"value": SECRET_VALUE})
    app_id = _deploy(a, bid).json()["app"]["id"]
    assert app_env(app_id)["WEATHER_API_KEY"] == (SECRET_VALUE, True)
    out = a.get(f"/apps/{app_id}").json()
    assert SECRET_VALUE not in str(out)
    again = _deploy(a, bid)
    assert again.status_code == 200 and app_env(app_id)["WEATHER_API_KEY"] == (SECRET_VALUE, True)
    # changing the value and the automatic (non-forced) deploy path still applies it, though the files are unchanged
    a.put(f"/kaisen/builds/{bid}/secrets/WEATHER_API_KEY", json={"value": SECRET_VALUE + "-new"})
    s = db()
    _, changed = deploy_mod.deploy_build(s, s.get(KaisenBuild, bid), fakes_registry(), force=False)
    assert changed is True and app_env(app_id)["WEATHER_API_KEY"] == (SECRET_VALUE + "-new", True)
    _, changed = deploy_mod.deploy_build(s, s.get(KaisenBuild, bid), fakes_registry(), force=False)
    assert changed is False


def fakes_registry():
    from app.providers.fake import FakeRegistry
    return FakeRegistry(default_size_bytes=1024)


def test_user_secret_overrides_same_named_app_var_and_delete_removes_it(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    app_id = _deploy(a, bid).json()["app"]["id"]
    assert a.patch(f"/apps/{app_id}", json={"env": [{"key": "MY_KEY", "value": "plain-old-value"}]}).status_code == 200
    a.put(f"/kaisen/builds/{bid}/secrets/MY_KEY", json={"value": SECRET_VALUE})
    _deploy(a, bid)
    assert app_env(app_id)["MY_KEY"] == (SECRET_VALUE, True)
    r = a.delete(f"/kaisen/builds/{bid}/secrets/MY_KEY")
    assert r.status_code == 200 and r.json()["secrets"] == []
    assert "MY_KEY" not in app_env(app_id) and db().query(KaisenSecret).count() == 0
    assert _audit("kaisen.secret_delete")[-1].detail == "name=MY_KEY"
    assert a.delete(f"/kaisen/builds/{bid}/secrets/MY_KEY").status_code == 404


def test_secret_value_redacted_in_transcript_file_and_logs(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid, files={"kaisen.json": '{"runtime": "static"}', "NOTES.md": f"key={SECRET_VALUE}"})
    a.put(f"/kaisen/builds/{bid}/secrets/MY_KEY", json={"value": SECRET_VALUE})
    assert SECRET_VALUE not in a.get(f"/kaisen/builds/{bid}/files/NOTES.md").text
    s = db()
    run = builder.latest_run(s, bid)
    run.messages = [{"role": "user", "content": f"my key is {SECRET_VALUE}"}]
    run.error = f"boom {SECRET_VALUE}"
    run.status = "error"
    s.commit()
    d = a.get(f"/kaisen/builds/{bid}")
    assert SECRET_VALUE not in d.text and "[REDACTED]" in d.text
    assert SECRET_VALUE not in a.get(f"/kaisen/builds/{bid}/download").content.decode("latin-1")
    # deploy_preview log snapshots use the same set
    from app.kaisen import settings as ksettings
    assert SECRET_VALUE in ksettings.load_for_build(s, s.get(KaisenBuild, bid)).secret_values()
    assert deploy_mod.sanitize(f"log {SECRET_VALUE}", ksettings.load_for_build(s, s.get(KaisenBuild, bid)).secret_values()) == "log [REDACTED]"


def test_model_sees_secret_names_never_values(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    a.put(f"/kaisen/builds/{bid}/secrets/WEATHER_API_KEY", json={"value": SECRET_VALUE})
    s = db()
    b_ = s.get(KaisenBuild, bid)
    _idle(bid)
    run2, _ = builder.start_feedback(s, b_, "make it blue", None, _uid("a@example.com"))
    text = run2.messages[0]["content"]
    assert "WEATHER_API_KEY" in text and SECRET_VALUE not in text
    assert "WEATHER_API_KEY" in builder.seed_message(b_, __import__("app.kaisen.buildsecrets", fromlist=["x"]).prompt_note(s, bid))["content"]


def test_deleting_the_build_deletes_its_secrets(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid)
    a.put(f"/kaisen/builds/{bid}/secrets/MY_KEY", json={"value": SECRET_VALUE})
    assert a.delete(f"/kaisen/builds/{bid}").status_code == 204
    assert db().query(KaisenSecret).count() == 0


# ---- F8b: postgres, bucket, volume --------------------------------------------------------------

def test_deploy_with_postgres_bucket_and_volume(team, monkeypatch):
    _, a, b, c, pid = team
    monkeypatch.setattr(config, "VOLUMES_ENABLED", True)
    manifest = '{"runtime": "python", "start": "python app.py", "database": "postgres", "bucket": true, "volume": {"path": "/data"}}'
    bid = _new_build(a, pid, files={"kaisen.json": manifest, "app.py": "print(1)"})
    r = _deploy(a, bid)
    assert r.status_code == 200, r.text
    app_id = r.json()["app"]["id"]
    out = a.get(f"/apps/{app_id}").json()
    assert out["want_db"] is True and out["db_engine"] == "pg" and out["want_storage"] is True
    env = {e["key"]: e["value"] for e in out["env"]}
    for k in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_CA_CERT", "DATABASE_URL", "DB_TLS"):
        assert env[k].startswith("${{ db."), k
    for k, ref in (("S3_ENDPOINT", "endpoint"), ("S3_REGION", "region"), ("S3_BUCKET", "bucket"),
                   ("S3_ACCESS_KEY", "access_key"), ("S3_SECRET_KEY", "secret_key")):
        assert env[k] == "${{ storage.%s }}" % ref
    row = db().get(models.App, app_id)
    assert row.volume_size_gi == config.VOLUME_DEFAULT_GI and row.volume_mount_path == "/data"


def test_mysql_deploy_has_no_pg_or_bucket_env(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid, files={"kaisen.json": '{"runtime": "python", "database": "mysql"}'})
    app_id = _deploy(a, bid).json()["app"]["id"]
    env = {e["key"] for e in a.get(f"/apps/{app_id}").json()["env"]}
    assert "DB_TLS" not in env and not any(k.startswith("S3_") for k in env)


@pytest.mark.parametrize("manifest,needle", [
    ('{"runtime": "python", "bucket": "yes"}', '"bucket" must be true or false'),
    ('{"runtime": "python", "volume": "/data"}', '"volume" must look like'),
    ('{"runtime": "python", "volume": {"path": "/data", "sizeGi": 5}}', '"volume" must look like'),
    ('{"runtime": "static", "database": "postgres"}', "static"),
    ('{"runtime": "static", "bucket": true}', "static"),
    ('{"database": "oracle"}', "must be one of mysql, postgres"),
])
def test_unsupported_manifest_combos_are_plain_422(team, manifest, needle):
    _, a, b, c, pid = team
    r = _deploy(a, _new_build(a, pid, files={"kaisen.json": manifest}))
    assert r.status_code == 422 and needle in r.json()["detail"], r.text
    assert db().query(models.App).count() == 0


def test_volume_needs_platform_support_and_valid_path(team, monkeypatch):
    _, a, b, c, pid = team
    bid = _new_build(a, pid, files={"kaisen.json": '{"runtime": "python", "volume": {"path": "/data"}}'})
    r = _deploy(a, bid)
    assert r.status_code == 422 and "volumes are not enabled" in r.json()["detail"].lower()
    monkeypatch.setattr(config, "VOLUMES_ENABLED", True)
    _add_files_replace(bid, '{"runtime": "python", "volume": {"path": "/etc"}}')
    r = _deploy(a, bid)
    assert r.status_code == 422 and '"volume" path' in r.json()["detail"]
    assert db().query(models.App).count() == 0


def _add_files_replace(bid, manifest):
    from app.kaisen.models import KaisenFile
    s = db()
    row = s.query(KaisenFile).filter_by(build_id=bid, path="kaisen.json").one()
    row.content, row.size = manifest, len(manifest)
    s.commit()


def test_resources_cannot_be_dropped_or_swapped_on_redeploy(team):
    _, a, b, c, pid = team
    bid = _new_build(a, pid, files={"kaisen.json": '{"runtime": "python", "database": "mysql", "bucket": true}'})
    app_id = _deploy(a, bid).json()["app"]["id"]
    _add_files_replace(bid, '{"runtime": "python", "database": "postgres", "bucket": true}')
    r = _deploy(a, bid)
    assert r.status_code == 422 and "engine cannot change" in r.json()["detail"]
    _add_files_replace(bid, '{"runtime": "python", "bucket": true}')
    assert "cannot be removed" in _deploy(a, bid).json()["detail"]
    _add_files_replace(bid, '{"runtime": "python", "database": "mysql"}')
    assert "bucket and it cannot be removed" in _deploy(a, bid).json()["detail"]
    row = db().get(models.App, app_id)
    assert row.want_db and row.want_storage and row.db_engine == "mysql"


def test_app_limit_still_applies_to_resource_previews(team):
    admin, a, b, c, pid = team
    assert admin.patch(f"/admin/projects/{pid}", json={"app_limit": 1}).status_code == 200
    first = _new_build(a, pid, name="one", files={"kaisen.json": '{"runtime": "python", "bucket": true}'})
    assert _deploy(a, first).status_code == 200
    second = _new_build(a, pid, name="two", files={"kaisen.json": '{"runtime": "python", "bucket": true}'})
    assert _deploy(a, second).status_code in (403, 409, 422)
    assert db().query(models.App).count() == 1


def test_builder_prompt_documents_new_resources_from_config():
    text = llm.build_system()
    assert '"postgres"' in text and '"bucket": true' in text and "S3_SECRET_KEY" in text
    assert f'"volume": {{"path": "{config.VOLUME_DEFAULT_MOUNT_PATH}"}}' in text
    assert f"{config.STORAGE_QUOTA_BYTES // (1024 * 1024)} MB" in text and "{{" not in text
    assert "PostgreSQL, object storage buckets, persistent volumes" not in text
