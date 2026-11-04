"""Coverage for the 2026-11-04 wave: error branches of the settings validation, domain parsing, output test, refusal
number formatting and the manifest reader the deploy path uses."""
import io

import pytest
from PIL import Image

from app import config
from app.imgproc import fmt, raster
from app.kaisen import deploy, pushing, settings as ks
from app.kaisen.github import FakeGitHub, GitHubError
from app.kaisen.models import KaisenFile
from tests.kaisen_helpers import KEY, TOKEN, db
from tests.test_kaisen_api import _new_build, _out, fakes, team  # noqa: F401
from tests.test_kaisen_push_jobs import world  # noqa: F401


def test_fmt_numbers_are_human():
    assert fmt.mb(5 * 1024 * 1024) == "5.0 MB"
    assert fmt.kb(2048) == "2 KB"
    assert fmt.megapixels(12_500_000) == "12.5 megapixels"


@pytest.mark.parametrize("value,expected", [
    ("docs.example.com, github.com/x/ \n docs.example.com", ["docs.example.com", "github.com/x"]),
    (["A.Example.COM", "  ", "a.example.com"], ["a.example.com"]),
    ("", []),
])
def test_parse_domains_ok(value, expected):
    assert ks.parse_domains(value) == expected


@pytest.mark.parametrize("value,msg", [
    (5, "must be a list"),
    (["https://x.example.com"], "is not a domain"),
    (["not a domain!"], "is not a domain"),
    ([f"d{i}.example.com" for i in range(config.KAISEN_WEB_DOMAINS_MAX + 1)], "At most"),
])
def test_parse_domains_rejects(value, msg):
    with pytest.raises(ValueError, match=msg):
        ks.parse_domains(value)


def test_settings_validation_and_clearing(world):
    _, a, _b, pid = world
    url = f"/projects/{pid}/kaisen"
    assert a.put(url, json={"reviewer": "sometimes"}).status_code == 422
    assert a.put(url, json={"reviewer_model": "gpt-nope"}).status_code == 422
    assert a.put(url, json={"web_allowed_domains": ["http://x.example.com"]}).status_code == 422
    assert a.put(url, json={"web_allowed_domains": ["Docs.Example.com/"]}).json()["web_allowed_domains"] == ["docs.example.com"]
    assert a.put(url, json={"anthropic_key": KEY, "github_token": TOKEN}).status_code == 200
    s = a.get(url).json()
    assert s["has_anthropic_key"] is True and ks.load(db(), pid).github_token == TOKEN
    r = a.put(url, json={"clear_anthropic_key": True, "clear_github_token": True})
    assert r.status_code == 200 and r.json()["has_anthropic_key"] is False and ks.load(db(), pid).github_token == ""


def test_web_access_must_be_a_boolean_in_save(world):
    _, a, _b, pid = world
    with pytest.raises(ValueError, match="web_access must be"):
        ks.save(db(), pid, 1, {"web_access": "yes"})
    with pytest.raises(ValueError, match="Unknown reviewer model"):
        ks.save(db(), pid, 1, {"reviewer_model": "nope"})
    with pytest.raises(ValueError, match="reviewer must be"):
        ks.save(db(), pid, 1, {"reviewer": "nope"})


def test_anthropic_test_without_a_key(world):
    _, a, _b, pid = world
    r = a.post(f"/projects/{pid}/kaisen/test/anthropic")
    assert r.status_code == 200 and r.json() == {"ok": False, "message": "No Anthropic API key is saved for this project yet."}


def test_output_test_messages(team, fakes):
    _, a, _b, _c, pid = team
    bid = _new_build(a, pid)
    t = lambda: a.post(f"/kaisen/builds/{bid}/output/test").json()  # noqa: E731
    assert t()["message"].startswith("Output is set to download only")
    a.put(f"/projects/{pid}/kaisen", json={"github_token": TOKEN})
    assert _out(a, bid, mode="github", repo="octo/demo").status_code == 200
    a.put(f"/projects/{pid}/kaisen", json={"clear_github_token": True})
    assert t()["message"].startswith("No access token is available")
    a.put(f"/projects/{pid}/kaisen", json={"github_token": TOKEN})
    assert t() == {"ok": True, "message": "OK: can push to octo/demo"}
    fakes.github.can_push = False
    r = t()
    assert r["ok"] is False and "no push permission" in r["message"] and TOKEN not in r["message"]


def test_default_push_runner_is_the_threaded_one():
    from app.routers.kaisen import get_push_runner
    assert get_push_runner() is pushing.threaded_runner


def test_push_build_is_synchronous_and_names_skipped_secrets(team, fakes):
    _, a, _b, _c, pid = team
    bid = _new_build(a, pid, files={"index.html": "<p>x</p>", ".env": "A=" + "b" * 12})
    a.put(f"/projects/{pid}/kaisen", json={"github_token": TOKEN})
    _out(a, bid, mode="github", repo="octo/demo")
    s = db()
    from app.kaisen.models import KaisenBuild
    b = s.get(KaisenBuild, bid)
    rs = ks.load_for_build(s, b)
    msg = pushing.push_build(s, b, ks.output_for(b, rs), lambda token: fakes.github)
    assert msg.startswith("Pushed to https://github.com/octo/demo") and "Not pushed because they look like secrets: .env" in msg
    assert ".env" not in fakes.github.pushes[-1]["files"]


def test_github_error_defaults():
    e = GitHubError("plain")
    assert (e.code, e.status) == ("github_error", None)
    with pytest.raises(GitHubError):
        FakeGitHub().check_repo("not a repo")


def test_oversize_header_unreadable_falls_back_to_the_limit_message():
    err = raster._too_many_pixels(b"not an image at all")
    assert err.code == "too_big" and "too many pixels" in err.message


def test_oversize_header_readable_reports_its_own_dimensions(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_PIXELS", 100)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_EDGE_IN", 10_000)
    buf = io.BytesIO()
    Image.new("RGB", (50, 50)).save(buf, "PNG")
    err = raster._too_many_pixels(buf.getvalue())
    assert "50 × 50" in err.message


@pytest.mark.parametrize("content,expect_db", [
    ("{not json", None),
    ("[1, 2]", None),
    ('{"runtime": "python", "database": "mysql"}', "mysql"),
])
def test_wanted_resources_reads_a_manifest_defensively(team, fakes, content, expect_db):
    _, a, _b, _c, pid = team
    bid = _new_build(a, pid, files={"kaisen.json": content})
    from app.kaisen.models import KaisenBuild
    s = db()
    b = s.get(KaisenBuild, bid)
    assert deploy.wanted_database(s, b) == expect_db
    s.query(KaisenFile).filter_by(build_id=bid, path="kaisen.json").delete()
    s.commit()
    assert deploy.wanted_resources(s, b).database is None


@pytest.mark.parametrize("manifest,msg", [
    ({"database": "oracle"}, "must be one of"),
    ({"bucket": "yes"}, "bucket"),
    ({"volume": "data"}, "volume"),
])
def test_manifest_values_the_deploy_would_refuse(manifest, msg):
    with pytest.raises(deploy.DeployError, match=msg):
        deploy.wanted_from_manifest(manifest)


def test_valid_output_rechecks_what_the_image_unit_returned():
    from app.kaisen import uploads
    assert uploads.valid_output("image/webp", b"RIFF\0\0\0\0WEBPxx")
    assert not uploads.valid_output("image/webp", b"RIFF\0\0\0\0WAVExx")
    assert uploads.valid_output("image/svg+xml", b"<svg xmlns='x'></svg>")
    assert not uploads.valid_output("image/svg+xml", b"<html>")
    assert not uploads.valid_output("image/png", b"")
    assert not uploads.valid_output("text/html", b"<svg ")
