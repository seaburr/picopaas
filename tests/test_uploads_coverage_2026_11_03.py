"""Coverage pass for the Kaisen uploads wave (2026-11-03): error paths in the image unit, its HTTP client, the router
and the shared rate limiter, plus the CSP middleware rule and the rolling-30-day spend boundary. Real register/login flow;
only the image unit is faked (it runs the real Pillow/SVG code)."""
import io
import socket
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

from app import config, models, ratelimit
from app.imgproc import core, raster, server, svg
from app.kaisen import builder, uploads as ku
from app.kaisen.models import KaisenAsset, KaisenBuild, KaisenEvent, KaisenUploadBlock
from app.providers.base import ImageProcessError
from app.providers.imgproc import HttpImageProcessor
from tests import imgfix
from tests.kaisen_helpers import db
from tests.test_kaisen_uploads_api import OCTET, proc, up, world  # noqa: F401 - fixtures

NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


# ---- SVG sanitizer edges --------------------------------------------------------------------------------------------------

def test_svg_keeps_xml_space_and_text_tails_and_drops_backslash_css():
    out = core.process(f'<svg {NS} width="10" height="10"><text xml:space="preserve" x="1" y="2">a<tspan>b</tspan>tail &amp; more</text>'
                       '<rect width="1" height="1" style="fill:\\72 ed"/></svg>'.encode(), "svg").data.decode()
    assert 'xml:space="preserve"' in out and "tail &amp; more" in out
    assert "\\72" not in out and "<rect " in out  # the attribute went, the shape stayed


def test_svg_size_falls_back_to_viewbox_and_survives_a_bad_one():
    ok = core.process(f'<svg {NS} viewBox="0 0 40.4 20"><rect width="1" height="1"/></svg>'.encode(), "svg")
    assert (ok.width, ok.height) == (40, 20)
    half = core.process(f'<svg {NS} width="7" viewBox="0 0 40 20"><rect width="1" height="1"/></svg>'.encode(), "svg")
    assert (half.width, half.height) == (7, 20)
    bad = core.process(f'<svg {NS} viewBox="0 0 wide 20"><rect width="1" height="1"/></svg>'.encode(), "svg")
    assert (bad.width, bad.height) == (None, None)


def test_svg_that_grows_while_cleaning_is_refused(monkeypatch):
    raw = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1"/></svg>'
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_BYTES", len(raw) + 10)  # fits in, but the rewrite adds the xlink declaration
    with pytest.raises(ImageProcessError) as ei:
        svg.sanitize(raw)
    assert ei.value.code == "too_big" and "after cleaning" in ei.value.message


def test_svg_text_node_writer_escapes():
    node = svg._Node("#text")
    node.text = "a<b&c"
    out: list[str] = []
    svg._write(node, out)
    assert out == ["a&lt;b&amp;c"]


# ---- raster edges ---------------------------------------------------------------------------------------------------------

def test_libwebp_version_parsing(monkeypatch):
    monkeypatch.setattr(raster.features, "version", lambda name: "1.x.y")
    assert raster.libwebp_ok() is False
    monkeypatch.setattr(raster.features, "version", lambda name: None)
    assert raster.libwebp_ok() is False
    monkeypatch.setattr(raster.features, "version", lambda name: "99.0.0")
    assert raster.libwebp_ok() is True


def test_old_libwebp_flattens_grayscale_and_translucent_sources(monkeypatch):
    monkeypatch.setattr(raster, "libwebp_ok", lambda: False)
    buf = io.BytesIO()
    Image.new("L", (8, 8), 90).save(buf, "WEBP", lossless=True)
    res = core.process(buf.getvalue(), "raster")  # L is not RGB: converted before the JPEG encode
    assert res.mime == "image/jpeg" and "Saved as jpeg" in res.notes


def test_processed_image_over_the_output_cap_is_refused(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_BYTES_OUT", 100)
    with pytest.raises(ImageProcessError) as ei:
        core.process(imgfix.png(200, 200), "raster")
    assert ei.value.code == "too_big" and "after processing" in ei.value.message


# ---- the image pod over HTTP ----------------------------------------------------------------------------------------------

def _client(handler):
    return HttpImageProcessor("http://pod", transport=httpx.MockTransport(handler))


def test_client_notes_must_be_a_json_list_and_are_clipped():
    def notes_object(request):
        return httpx.Response(200, content=b"x", headers={"X-Mime": "image/png", "X-Notes": '{"a": 1}'})
    with pytest.raises(ImageProcessError) as ei:
        _client(notes_object).process(b"x", "raster")
    assert ei.value.code == "unavailable"

    def many(request):
        return httpx.Response(200, content=b"x", headers={"X-Mime": "image/png", "X-Width": "3", "X-Height": "",
                                                          "X-Notes": '[' + ",".join(['"' + "n" * 300 + '"'] * 15) + ']'})
    res = _client(many).process(b"x", "raster")
    assert len(res.notes) == 10 and all(len(n) == 200 for n in res.notes) and (res.width, res.height) == (3, None)


@pytest.mark.parametrize("exc,code", [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("down"), "unavailable"),
                                      (httpx.RemoteProtocolError("bad"), "unavailable")])
def test_client_maps_transport_failures(exc, code):
    def boom(request):
        raise exc
    with pytest.raises(ImageProcessError) as ei:
        _client(boom).process(b"x", "raster")
    assert ei.value.code == code


@pytest.mark.parametrize("status", [500, 502, 503])
def test_client_treats_5xx_without_a_json_body_as_unavailable(status):
    with pytest.raises(ImageProcessError) as ei:
        _client(lambda r: httpx.Response(status, content=b"<html>bad gateway</html>")).process(b"x", "raster")
    assert ei.value.code == "unavailable"


def test_client_passes_through_known_5xx_codes_and_json_with_wrong_shape():
    got = _client(lambda r: httpx.Response(504, json={"code": "timeout", "message": "slow"}))
    with pytest.raises(ImageProcessError) as ei:
        got.process(b"x", "raster")
    assert (ei.value.code, ei.value.message) == ("timeout", "slow")
    for body in ([1, 2], {"code": "busy"}, "text"):
        with pytest.raises(ImageProcessError) as ei:
            _client(lambda r, b=body: httpx.Response(503, json=b)).process(b"x", "raster")
        assert ei.value.code == "unavailable"


def test_client_stops_reading_an_oversized_reply_and_never_follows_redirects(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_BYTES_OUT", 50)
    with pytest.raises(ImageProcessError) as ei:
        _client(lambda r: httpx.Response(200, content=b"x" * 5000, headers={"X-Mime": "image/png"})).process(b"x", "raster")
    assert ei.value.code == "too_big"
    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://elsewhere.invalid/"})
    with pytest.raises(ImageProcessError):
        _client(redirect).process(b"x", "raster")
    assert seen == ["http://pod/process?hint=raster"]


@pytest.fixture()
def pod():
    import threading
    srv = server.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_server_reports_a_body_cut_short(pod):
    s = socket.create_connection(("127.0.0.1", pod), timeout=5)
    s.sendall(b"POST /process?hint=raster HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\nabc")
    s.shutdown(socket.SHUT_WR)
    reply = b""
    while (part := s.recv(4096)):
        reply += part
    s.close()
    assert b"422" in reply.split(b"\r\n")[0] and b"cut short" in reply


def test_server_arms_and_clears_the_alarm_when_the_handler_is_installed(pod, monkeypatch):
    calls = []
    monkeypatch.setattr(server.signal, "alarm", calls.append)  # never arm a real SIGALRM in the test process
    monkeypatch.setattr(server, "_alarm_ready", True)
    r = httpx.post(f"http://127.0.0.1:{pod}/process?hint=raster", content=imgfix.png(8, 8))
    assert r.status_code == 200 and calls == [config.KAISEN_UPLOAD_IMG_TIMEOUT, 0]


# ---- router: bad headers, blocklist on the stored hash, filename on replace --------------------------------------------

def test_a_garbage_content_length_is_a_400(world):
    r = world.a.post(f"/kaisen/builds/{world.bid}/uploads", params={"kind": "asset", "filename": "a.png"},
                     content=imgfix.png(8, 8), headers={**OCTET, "Content-Length": "abc"})
    assert r.status_code == 400 and "Content-Length" in r.json()["detail"]


def test_blocklisted_stored_hash_is_refused_even_if_the_original_differs(world):
    first = up(world.a, world.bid, imgfix.png(10, 10, text={"k": "v"}))
    assert first.status_code == 201
    stored = first.json()["sha256"]
    s = db()
    s.query(KaisenAsset).delete()
    s.add(KaisenUploadBlock(sha256=stored, reason="test", added_by=None))
    s.commit()
    again = up(world.a, world.bid, imgfix.png(10, 10, text={"k": "other"}))  # different bytes in, same pixels out
    assert again.status_code == 422 and again.json()["detail"] == ku.BLOCKED


def test_replace_with_a_filename_updates_the_display_name_only(world):
    uid = up(world.a, world.bid, imgfix.png(10, 10), filename="logo.png").json()["id"]
    r = world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", params={"filename": "My New Logo.png"},
                    content=imgfix.png(12, 12), headers=OCTET)
    assert r.status_code == 200 and r.json()["name_orig"] == "My New Logo.png" and r.json()["path"] == "uploads/logo.png"


# ---- kill switch x project flag matrix -------------------------------------------------------------------------------------

@pytest.mark.parametrize("platform,project,status", [(True, True, 201), (True, False, 403), (False, True, 403), (False, False, 403)])
def test_write_needs_both_the_platform_switch_and_the_project_flag(world, platform, project, status):
    assert world.admin.put("/admin/settings", json={"settings": {"kaisen_uploads_enabled": platform}}).status_code == 200
    assert world.admin.put(f"/admin/projects/{world.pid}/kaisen-uploads", json={"enabled": project}).status_code == 200
    r = up(world.a, world.bid, imgfix.png(8, 8))
    assert r.status_code == status
    if status == 403:
        assert r.json()["detail"] == (ku.SWITCHED_OFF if not platform else ku.NOT_ENABLED)  # the kill switch is reported first
    listing = world.a.get(f"/kaisen/builds/{world.bid}/uploads").json()  # reading never needs either
    assert listing["uploads_enabled"] is (platform and project)
    assert (listing["uploads_platform_enabled"], listing["uploads_project_enabled"]) == (platform, project)


# ---- reference nonce, valid_output ---------------------------------------------------------------------------------------

def test_read_reference_picks_a_new_nonce_when_the_content_holds_the_first(world, monkeypatch):
    up(world.a, world.bid, b"collide-aaaaaaaaaaaa here", kind="reference", filename="n.txt")
    seq = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    monkeypatch.setattr(ku.secrets, "token_hex", lambda n: next(seq))
    out = ku.read_reference(db(), world.bid, "n.txt")
    assert 'reference id="bbbbbbbbbbbb"' in out


@pytest.mark.parametrize("mime,data,ok", [
    ("image/webp", b"RIFF\0\0\0\0WEBPxxxx", True), ("image/webp", b"RIFF\0\0\0\0WAVExxxx", False),
    ("image/png", b"", False), ("text/html", b"<svg ", False), ("image/svg+xml", b"<?xml ?><svg ", False),
    ("image/svg+xml", b"<svg a/>", True), ("image/jpeg", b"\x89PNG\r\n\x1a\n", False),
])
def test_valid_output_checks_magic_and_type(mime, data, ok):
    assert ku.valid_output(mime, data) is ok


# ---- shared rate limiter ---------------------------------------------------------------------------------------------------

def _req(ip="203.0.113.7"):
    return Request({"type": "http", "headers": [(b"x-forwarded-for", ip.encode())], "client": ("10.0.0.1", 1)})


def test_upload_limit_is_per_user_project_and_ip_and_fails_open(db_session, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_RATE_MAX", 2)
    ratelimit.check_upload(db_session, _req(), 1, 1)
    ratelimit.check_upload(db_session, _req(), 1, 2)  # same user and IP, other project
    with pytest.raises(HTTPException) as ei:
        ratelimit.check_upload(db_session, _req("198.51.100.9"), 1, 3)  # same user, new IP and project
    assert ei.value.status_code == 429 and int(ei.value.headers["Retry-After"]) > 0
    ratelimit.check_upload(db_session, _req("198.51.100.9"), 2, 4)  # everything new: fine

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ratelimit, "_remaining", broken)
    ratelimit.check_upload(db_session, _req(), 1, 1)  # fails open


def test_bundle_fetch_limit_and_window_rollover(db_session, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "KAISEN_BUNDLE_FETCH_MAX", 1)
    ratelimit.check_bundle_fetch(db_session, _req())
    with pytest.raises(HTTPException):
        ratelimit.check_bundle_fetch(db_session, _req())
    row = db_session.query(models.AuthThrottle).first()
    row.window_start = row.window_start - timedelta(seconds=config.KAISEN_BUNDLE_FETCH_WINDOW_SECONDS + 5)
    db_session.commit()
    ratelimit.check_bundle_fetch(db_session, _req())  # expired window restarts the count
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    ratelimit.check_bundle_fetch(db_session, _req())
    ratelimit.check_upload(db_session, _req(), 1, 1)


def test_bump_survives_a_lost_insert_race(db_session, monkeypatch):
    k = ratelimit._key("race", "x")
    real_commit = db_session.commit
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("duplicate key")  # the other replica inserted first
        real_commit()
    db_session.add(models.AuthThrottle(key=k, window_start=ratelimit._now(), count=1))
    real_commit()
    db_session.expunge_all()
    monkeypatch.setattr(db_session, "get", lambda *a, **kw: None)  # we believe the row is absent
    monkeypatch.setattr(db_session, "commit", flaky)
    ratelimit._bump(db_session, k, 60)
    monkeypatch.undo()
    assert db_session.get(models.AuthThrottle, k).count == 2


# ---- CSP middleware and spend window ------------------------------------------------------------------------------------

def test_global_csp_applies_everywhere_but_upload_content_keeps_its_own(world):
    up(world.a, world.bid, imgfix.png(8, 8))
    uid = world.a.get(f"/kaisen/builds/{world.bid}/uploads").json()["uploads"][0]["id"]
    own = world.a.get(f"/kaisen/builds/{world.bid}/uploads/{uid}/content")
    assert own.headers["content-security-policy"] == "default-src 'none'; sandbox"
    other = world.a.get(f"/kaisen/builds/{world.bid}/uploads")
    assert other.headers["content-security-policy"] != own.headers["content-security-policy"]
    assert "default-src" in other.headers["content-security-policy"] and other.headers["x-frame-options"] == "DENY"
    assert own.headers["x-frame-options"] == "DENY" and own.headers["x-content-type-options"] == "nosniff"


def test_spend_last30_window_edges(world):
    from app.kaisen.models import KaisenRun
    s = db()
    b = s.get(KaisenBuild, world.bid)
    run = KaisenRun(build_id=b.id, kind="build", status="done", messages=[])
    s.add(run)
    s.flush()
    now = builder._now()
    for days, usd in ((0, 1.0), (29.9, 2.0), (30.1, 4.0), (45, 8.0)):
        s.add(KaisenEvent(run_id=run.id, build_id=b.id, project_id=b.project_id, model="m", usd=usd, at=now - timedelta(days=days)))
    s.commit()
    assert round(builder.spend_last30(s, b.project_id), 6) == 3.0
    assert builder.spend_last30(s, b.project_id + 999) == 0


def test_grayscale_jpeg_is_reencoded_as_rgb():
    buf = io.BytesIO()
    Image.new("L", (9, 9), 77).save(buf, "JPEG")
    res = core.process(buf.getvalue(), "raster")
    assert res.mime == "image/jpeg" and Image.open(io.BytesIO(res.data)).mode == "RGB"


def test_a_translucent_image_headed_for_jpeg_is_composited_on_white(monkeypatch):
    # Defense in depth: no real JPEG decodes to RGBA, so force the normalizer to hand one over.
    monkeypatch.setattr(raster, "_normalize", lambda img: Image.new("RGBA", img.size, (0, 0, 0, 0)))
    buf = io.BytesIO()
    Image.new("RGB", (6, 6), (1, 2, 3)).save(buf, "JPEG")
    res = core.process(buf.getvalue(), "raster")
    px = Image.open(io.BytesIO(res.data)).convert("RGB").getpixel((3, 3))
    assert res.mime == "image/jpeg" and min(px) > 240  # transparent became white


# ---- builder: tool argument errors and run-start guards (same Workspace the uploads wave touched) -------------------------

from app.kaisen.paths import ToolError  # noqa: E402
from tests.test_kaisen_uploads_agent import _ws, w  # noqa: E402,F401 - fixtures


@pytest.mark.parametrize("args,msg", [
    (dict(pattern=""), "non-empty"), (dict(pattern=5), "non-empty"), (dict(pattern="x", path_prefix=3), "path_prefix"),
    (dict(pattern="x" * 5000), "too long"), (dict(pattern="(", regex=True), "regex"),
])
def test_search_tool_argument_errors(w, args, msg):  # noqa: F811
    with pytest.raises(ToolError) as ei:
        _ws(w).search(**args)
    assert msg in str(ei.value)


def test_search_stops_at_the_deadline_and_reports_it(w, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "KAISEN_SEARCH_DEADLINE_SECONDS", -1)
    out = _ws(w).search("index")
    assert out == "search stopped: too slow"


def test_stop_reason_names_a_deleted_or_suspended_starter(w):  # noqa: F811
    from app.kaisen.models import KaisenRun
    run = KaisenRun(build_id=w.bid, kind="build", status="queued", messages=[])
    w.s.add(run)
    w.s.commit()
    assert builder.stop_reason(w.s, run) is None
    owner = w.s.get(models.User, w.s.get(KaisenBuild, w.bid).created_by)
    owner.status = "suspended"
    w.s.commit()
    assert "suspended" in builder.stop_reason(w.s, run)
    owner.status = "rejected"
    w.s.commit()
    assert "rejected" in builder.stop_reason(w.s, run) and "suspended" not in builder.stop_reason(w.s, run)
    owner.status = "pending"
    w.s.commit()
    assert "not approved yet" in builder.stop_reason(w.s, run)
    owner.status = "deleted"
    w.s.commit()
    assert "no longer has an account" in builder.stop_reason(w.s, run)


def test_cutoff_note_is_added_as_a_new_user_turn_when_the_last_turn_is_the_model(w):  # noqa: F811
    msgs = builder._with_cutoff_note([{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}])
    assert msgs[-1]["role"] == "user" and builder.CUTOFF_MARK in msgs[-1]["content"][0]["text"]
    assert builder._with_cutoff_note(None)[-1]["role"] == "user"


def test_uploads_migration_round_trip_and_matches_the_models(tmp_path):
    import os
    import subprocess
    import sys
    from sqlalchemy import create_engine, inspect
    url = f"sqlite:///{tmp_path}/m.db"
    root = os.path.join(os.path.dirname(__file__), "..")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True,
                              capture_output=True, text=True).stdout

    def insp():
        return inspect(create_engine(url))
    alembic("upgrade", "head")
    assert len(alembic("heads").strip().splitlines()) == 1
    i = insp()
    assert "kaisen_uploads_enabled" in {c["name"] for c in i.get_columns("projects")}
    for model in (KaisenAsset, KaisenUploadBlock):
        assert {c["name"] for c in i.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    alembic("downgrade", "a9c4e1f7b2d6")
    i = insp()
    assert "kaisen_uploads_enabled" not in {c["name"] for c in i.get_columns("projects")}
    assert "kaisen_assets" not in i.get_table_names() and "kaisen_upload_blocklist" not in i.get_table_names()
    alembic("upgrade", "head")
    assert "kaisen_assets" in insp().get_table_names()


def test_module_entry_point_calls_the_server_main(monkeypatch):
    import runpy
    called = []
    monkeypatch.setattr(server, "main", lambda: called.append(1))
    runpy.run_module("app.imgproc", run_name="__main__")
    assert called == [1]
