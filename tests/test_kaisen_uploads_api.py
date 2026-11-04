"""Kaisen file uploads API (design section 16 + review 16.8): per-project admin flag, raw-body uploads, caps, rate limit,
authz, takedown. Real register/login flow; only the image unit is faked (it runs the real Pillow/SVG code in-process)."""
import asyncio
import hashlib
import io
import tarfile

import pytest
from PIL import Image
from sqlalchemy import event

from app import config, models
from app.kaisen import uploads as ku
from app.kaisen.models import KaisenAsset, KaisenBundle, KaisenRun, KaisenUploadBlock
from app.main import app as fastapi_app
from app.providers.base import ImageProcessError, ProcessedImage
from app.providers.fake import FakeImageProcessor
from app.routers.kaisen_uploads import get_image_processor
from tests import imgfix
from tests.conftest import register_and_login
from tests.kaisen_helpers import db

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><script>alert(1)</script><rect width="5" height="5"/></svg>'
OCTET = {"Content-Type": "application/octet-stream"}


def _user(make_client, email):
    c = make_client()
    register_and_login(c, email)
    return c


def _pid(c):
    return next(p["id"] for p in c.get("/projects").json() if p["is_personal"])


@pytest.fixture()
def proc():
    fake = FakeImageProcessor()
    fastapi_app.dependency_overrides[get_image_processor] = lambda: fake
    return fake


@pytest.fixture()
def world(make_client, proc):
    """admin (first user), owner a with a personal project (flag ON), member b of a team project (flag ON), outsider c."""
    admin = _user(make_client, "admin@example.com")
    a, b, c = (_user(make_client, f"{n}@example.com") for n in "abc")
    pid = _pid(a)
    assert admin.put(f"/admin/projects/{pid}/kaisen-uploads", json={"enabled": True}).status_code == 200
    bid = a.post("/kaisen/builds", json={"project_id": pid, "name": "Site", "brief": "A marketing site"}).json()["id"]
    return type("W", (), {"admin": admin, "a": a, "b": b, "c": c, "pid": pid, "bid": bid, "proc": proc})


def up(c, bid, data, kind="asset", filename="logo.png", **q):
    return c.post(f"/kaisen/builds/{bid}/uploads", params={"kind": kind, "filename": filename, **q}, content=data, headers=OCTET)


def rows():
    s = db()
    s.expire_all()
    return s.query(KaisenAsset).order_by(KaisenAsset.id).all()


def audits(action):
    s = db()
    s.expire_all()
    return s.query(models.AuditLog).filter_by(action=action).all()


# ---- flag, kill switch, settings view -------------------------------------------------------------------------------

def test_flag_off_by_default_refuses_uploads_with_a_clear_403(make_client, proc):
    _user(make_client, "admin@example.com")
    a = _user(make_client, "a@example.com")
    pid = _pid(a)
    bid = a.post("/kaisen/builds", json={"project_id": pid, "name": "Site", "brief": "x"}).json()["id"]
    r = up(a, bid, imgfix.png())
    assert r.status_code == 403 and "not enabled for this project" in r.json()["detail"]
    assert proc.calls == [] and rows() == []
    assert a.put(f"/kaisen/builds/{bid}/uploads/1", content=imgfix.png(), headers=OCTET).status_code == 403
    listing = a.get(f"/kaisen/builds/{bid}/uploads")
    assert listing.status_code == 200 and listing.json()["uploads"] == []
    assert listing.json()["uploads_enabled"] is False and listing.json()["uploads_disabled_reason"]


def test_settings_and_build_detail_expose_the_flag_read_only(world):
    s = world.a.get(f"/projects/{world.pid}/kaisen").json()
    assert s["uploads_enabled"] is True and s["uploads_project_enabled"] is True and s["uploads_platform_enabled"] is True
    assert s["uploads_notice"] == ku.PUBLIC_NOTICE and "public once it is deployed" in s["uploads_notice"]
    assert world.a.put(f"/projects/{world.pid}/kaisen", json={"uploads_enabled": False}).status_code == 422  # extra=forbid
    d = world.a.get(f"/kaisen/builds/{world.bid}").json()["uploads"]
    assert d["uploads_enabled"] is True and d["count"] == 0 and d["max_files"] == config.KAISEN_UPLOAD_MAX_FILES


def test_settings_show_not_enabled_label_when_off(make_client, proc):
    _user(make_client, "admin@example.com")
    a = _user(make_client, "a@example.com")
    s = a.get(f"/projects/{_pid(a)}/kaisen").json()
    assert s["uploads_enabled"] is False and s["uploads_not_enabled_label"] == "Not enabled for this project yet"


def test_only_a_platform_admin_can_set_the_project_flag_and_it_is_audited(world):
    url = f"/admin/projects/{world.pid}/kaisen-uploads"
    assert world.a.put(url, json={"enabled": False}).status_code == 403  # the project owner is not enough
    assert world.c.put(url, json={"enabled": False}).status_code == 403
    anon = world.admin.__class__(fastapi_app)
    assert anon.put(url, json={"enabled": True}).status_code == 401
    assert world.admin.put("/admin/projects/99999/kaisen-uploads", json={"enabled": True}).status_code == 404
    assert world.admin.put(url, json={"enabled": "maybe"}).status_code == 422
    assert world.admin.put(url, json={}).status_code == 422
    r = world.admin.put(url, json={"enabled": False})
    assert r.status_code == 200 and r.json()["kaisen_uploads_enabled"] is False
    assert world.admin.get(url).json()["kaisen_uploads_enabled"] is False
    assert up(world.a, world.bid, imgfix.png()).status_code == 403
    assert "enabled=False" in audits("kaisen.uploads_flag")[-1].detail and len(audits("kaisen.uploads_flag")) == 2


def test_admin_project_list_carries_the_flag(world):
    shared = world.a.post("/projects", json={"name": "Team"}).json()["id"]
    world.admin.put(f"/admin/projects/{shared}/kaisen-uploads", json={"enabled": True})
    row = next(p for p in world.admin.get("/admin/projects").json() if p["id"] == shared)
    assert row["kaisen_uploads_enabled"] is True


def test_kill_switch_blocks_post_and_put_only(world):
    assert up(world.a, world.bid, imgfix.png()).status_code == 201
    uid = rows()[0].id
    assert world.admin.put("/admin/settings", json={"settings": {"kaisen_uploads_enabled": False}}).status_code == 200
    r = up(world.a, world.bid, imgfix.png())
    assert r.status_code == 403 and "switched off" in r.json()["detail"]
    assert world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.png(), headers=OCTET).status_code == 403
    assert world.a.get(f"/kaisen/builds/{world.bid}/uploads").status_code == 200
    assert world.a.get(f"/kaisen/builds/{world.bid}/uploads/{uid}/content").status_code == 200
    assert world.a.delete(f"/kaisen/builds/{world.bid}/uploads/{uid}").status_code == 204


# ---- happy path and what is stored -----------------------------------------------------------------------------------

def test_png_upload_is_reencoded_stored_audited_and_listed(world):
    src = imgfix.png(64, 48, text={"Comment": "meta"})
    r = up(world.a, world.bid, src, description="  company\nlogo <b> ", filename="My Logo.PNG")
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["path"] == "uploads/my-logo.png" and j["name"] == "my-logo.png" and j["kind"] == "asset" and j["mime"] == "image/png"
    assert (j["width"], j["height"], j["version"]) == (64, 48, 1) and j["description"] == "company logo b"
    assert j["name_orig"] == "My Logo.PNG" and "Metadata removed" in j["notes"] and "data" not in j
    [row] = rows()
    assert row.data != src and b"tEXt" not in row.data and row.sha256 == hashlib.sha256(row.data).hexdigest()
    assert row.orig_sha256 == hashlib.sha256(src).hexdigest() and row.size == len(row.data) == j["size"]
    assert row.created_by == world.a.get("/auth/me").json()["id"]
    assert world.proc.calls == [(len(src), "raster")]
    [a] = audits("kaisen.upload_add")
    assert "path=uploads/my-logo.png" in a.detail and row.sha256[:12] in a.detail and "My Logo" not in a.detail
    got = world.a.get(f"/kaisen/builds/{world.bid}/uploads").json()
    assert [u["id"] for u in got["uploads"]] == [row.id] and got["quota"]["count"] == 1 and got["notice"] == ku.PUBLIC_NOTICE
    assert world.a.get(f"/kaisen/builds/{world.bid}").json()["uploads"]["asset_count"] == 1


def test_jpeg_exif_and_gps_never_reach_storage_and_extension_is_canonical(world):
    r = up(world.a, world.bid, imgfix.jpeg(), filename="holiday.JPEG")
    assert r.status_code == 201 and r.json()["path"] == "uploads/holiday.jpg" and r.json()["mime"] == "image/jpeg"
    stored = rows()[0].data
    assert b"CameraMakerXYZ" not in stored and len(imgfix.opened(stored).getexif()) == 0


def test_webp_and_svg_assets(world):
    assert up(world.a, world.bid, imgfix.webp(), filename="a.webp").json()["mime"] == "image/webp"
    r = up(world.a, world.bid, SVG, filename="icon.svg")
    assert r.status_code == 201 and r.json()["path"] == "uploads/icon.svg" and r.json()["mime"] == "image/svg+xml"
    assert (r.json()["width"], r.json()["height"]) == (20, 10)
    svg = next(x for x in rows() if x.ext == "svg")
    assert b"script" not in svg.data and b"alert" not in svg.data and b"<rect" in svg.data


def test_filename_is_only_a_label_names_are_generated(world):
    names = {}
    for label in ("../../etc/passwd.png", "..\\..\\evil.png", "CON.png", "uploads.png", "‮gnp.exe.png", "ünï cödé.png", "",
                  "x" * 290 + ".png", "kaisen.json.png", "a.b.c.d.png"):
        r = up(world.a, world.bid, imgfix.png(8, 8), filename=label)
        assert r.status_code == 201, (label, r.text)
        names[label] = r.json()["path"]
    assert names["../../etc/passwd.png"] == "uploads/passwd.png"  # directories in the label are ignored
    assert names["..\\..\\evil.png"] == "uploads/evil.png"
    assert names["CON.png"] == "uploads/image.png" and names["uploads.png"] == "uploads/image-2.png"
    assert names["ünï cödé.png"] == "uploads/uni-code.png" and names[""] == "uploads/image-3.png"
    assert len(names["x" * 290 + ".png"].split("/")[1]) <= config.KAISEN_UPLOAD_NAME_MAX + 4
    assert names["kaisen.json.png"] == "uploads/kaisen-json.png" and names["a.b.c.d.png"] == "uploads/a-b-c-d.png"
    assert names["\u202egnp.exe.png"] == "uploads/gnp-exe.png"
    assert all(p.startswith("uploads/") and p.count("/") == 1 and p.isascii() and p == p.lower() for p in names.values())
    assert len({p.lower() for p in names.values()}) == len(names)  # duplicates got -2, -3 ...
    for row in rows():
        assert ".." not in row.path and "‮" not in row.name_orig


def test_duplicate_names_get_a_counter(world):
    p = [up(world.a, world.bid, imgfix.png(8, 8), filename="hero.png").json()["path"] for _ in range(3)]
    assert p == ["uploads/hero.png", "uploads/hero-2.png", "uploads/hero-3.png"]


def test_collision_with_a_generated_file_is_a_409(world):
    from app.kaisen.models import KaisenFile
    s = db()
    s.add(KaisenFile(build_id=world.bid, path="uploads/logo.png", content="x", size=1))
    s.commit()
    r = up(world.a, world.bid, imgfix.png(8, 8))
    assert r.status_code == 409 and "uploads/logo.png" in r.json()["detail"]


# ---- references ------------------------------------------------------------------------------------------------------

def test_text_reference_is_stored_untouched_never_served_inline(world):
    css = b"body { color: #333; }\n"
    r = up(world.a, world.bid, css, kind="reference", filename="Old Site.css", description="current stylesheet")
    assert r.status_code == 201 and r.json()["path"] == "references/old-site.css" and r.json()["ext"] == "css"
    assert world.proc.calls == []  # no decode for text
    [row] = rows()
    assert row.data == css and row.kind == "reference"
    c = world.a.get(f"/kaisen/builds/{world.bid}/uploads/{row.id}/content")
    assert c.status_code == 200 and c.content == css
    assert c.headers["content-type"].startswith("text/plain") and c.headers["x-content-type-options"] == "nosniff"
    assert c.headers["content-disposition"] == 'attachment; filename="old-site.css"'
    assert c.headers["content-security-policy"] == "default-src 'none'; sandbox" and "no-store" in c.headers["cache-control"]
    assert "Old Site" not in str(c.headers)


@pytest.mark.parametrize("name,body,ext", [("a.md", b"# hi", "md"), ("n.txt", b"notes", "txt"), ("p.html", b"<p>x</p>", "html"),
                                           ("noext", b"<!DOCTYPE html><title>x</title>", "html"), ("weird.bin", b"just words", "txt")])
def test_reference_types(world, name, body, ext):
    r = up(world.a, world.bid, body, kind="reference", filename=name)
    assert r.status_code == 201 and r.json()["ext"] == ext


def _fake_secrets():
    return {
        "private key": "-----BEGIN " + "RSA PRIVATE KEY-----\nabc\n",
        "Anthropic": "key = sk-" + "ant-" + "a1" * 12,
        "PicoPaaS": "pp_" + "sk_" + "Ab3" * 8,
        "GitHub": "tok " + "gh" + "p_" + "A1" * 15,
        "AWS": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "JWT": "ey" + "J" + "abcdefghij.ey" + "J" + "abcdefghij.abcdefghij",
        "assignment": "DB_PASSWORD = 'Zz9" + "q" * 16 + "'",
        "base64": "config: " + __import__("base64").b64encode(("password=Qq7" + "w" * 60).encode()).decode(),
    }


@pytest.mark.parametrize("which", list(_fake_secrets()))
def test_reference_with_a_secret_is_refused_and_the_value_is_not_echoed(world, which):
    text = _fake_secrets()[which]
    r = up(world.a, world.bid, text.encode(), kind="reference", filename="env.txt")
    assert r.status_code == 422 and "looks like it contains" in r.json()["detail"]
    assert text[10:30] not in r.text and rows() == []
    assert any("reason=secret" in a.detail for a in audits("kaisen.upload_rejected"))


def test_description_with_a_secret_is_refused(world):
    r = up(world.a, world.bid, imgfix.png(8, 8), description="key sk-" + "ant-" + "z9" * 12)
    assert r.status_code == 422 and rows() == []


def test_innocent_css_custom_properties_are_not_flagged(world):
    css = b":root{--token-color: var(--brand-primary-color); --secret-sauce: 12px;} .a{color:var(--token-color)}"
    assert up(world.a, world.bid, css, kind="reference", filename="v.css").status_code == 201


def test_binary_or_oversized_text_reference_is_refused(world):
    assert up(world.a, world.bid, b"abc\x00def", kind="reference").status_code == 422
    assert up(world.a, world.bid, b"\xff\xfe\xfa binary", kind="reference").status_code == 422
    assert up(world.a, world.bid, b"a" * (config.KAISEN_UPLOAD_TEXT_BYTES + 1), kind="reference").status_code == 413


# ---- type rules ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data", [b"PK\x03\x04" + b"0" * 50, b"\x1f\x8b\x08" + b"0" * 50, b"%PDF-1.7\n", b"GIF89a" + b"0" * 20,
                                  b"<html><script>alert(1)</script></html>", b"MZ\x90\x00" + b"0" * 20, b"plain text, no image", b""])
def test_non_images_are_refused_as_assets(world, data):
    r = up(world.a, world.bid, data, filename="x.png")
    assert r.status_code == 415 and rows() == []


def test_image_as_reference_and_svg_as_reference_are_refused(world):
    assert up(world.a, world.bid, imgfix.png(), kind="reference").status_code == 415
    assert up(world.a, world.bid, SVG, kind="reference").status_code == 415


def test_disguised_html_named_png_never_stored(world):
    r = up(world.a, world.bid, b"<html><body onload=alert(1)></body></html>", filename="cat.png")
    assert r.status_code == 415 and rows() == []


def test_image_unit_errors_map_to_http_statuses_and_audit(world):
    big = Image.new("RGB", (4000, 4000))
    buf = io.BytesIO()
    big.save(buf, "PNG")
    r = up(world.a, world.bid, buf.getvalue())
    assert r.status_code == 422 and "pixels" in r.json()["detail"]
    assert up(world.a, world.bid, imgfix.animated_webp()).status_code == 422
    assert up(world.a, world.bid, imgfix.png(200, 200)[:100]).status_code == 422
    assert [a.detail for a in audits("kaisen.upload_rejected")] == ["reason=bomb", "reason=bomb", "reason=corrupt"]
    world.proc.fail_with = ImageProcessError("unavailable", "Image processing isn't available right now.")
    assert up(world.a, world.bid, imgfix.png()).status_code == 503
    world.proc.fail_with = ImageProcessError("timeout", "That file took too long to process.")
    assert up(world.a, world.bid, imgfix.png()).status_code == 504
    assert rows() == []


def test_the_apis_distrusts_what_the_image_unit_returns(world):
    class Liar:
        def process(self, data, hint):
            return ProcessedImage(mime="image/png", data=b"<html>not a png</html>", width=1, height=1)

    fastapi_app.dependency_overrides[get_image_processor] = lambda: Liar()
    assert up(world.a, world.bid, imgfix.png()).status_code == 422
    fastapi_app.dependency_overrides[get_image_processor] = lambda: type("L", (), {"process": lambda s, d, h: ProcessedImage("text/html", d)})()
    assert up(world.a, world.bid, imgfix.png()).status_code == 422 and rows() == []


def test_processing_queue_is_bounded(world, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_IMG_QUEUE", 0)
    r = up(world.a, world.bid, imgfix.png())
    assert r.status_code == 503 and "busy" in r.json()["detail"] and world.proc.calls == []
    monkeypatch.setattr(config, "KAISEN_UPLOAD_IMG_QUEUE", 2)
    assert up(world.a, world.bid, imgfix.png()).status_code == 201 and ku.gate._n == 0


# ---- raw body size limits (R3) -----------------------------------------------------------------------------------------

def test_declared_content_length_over_the_cap_is_413_before_any_work(world):
    r = up(world.a, world.bid, b"\x89PNG\r\n\x1a\n" + b"0" * config.KAISEN_UPLOAD_MAX_BYTES_IN)
    assert r.status_code == 413 and "most allowed is 4 MB" in r.json()["detail"] and world.proc.calls == []


def test_chunked_body_over_the_cap_is_cut_off_while_streaming(world):
    pulled = []

    def gen():
        for i in range(40):
            pulled.append(i)
            yield b"0" * (config.KAISEN_UPLOAD_MAX_BYTES_IN // 8)

    r = world.a.post(f"/kaisen/builds/{world.bid}/uploads", params={"kind": "asset"}, content=gen(), headers=OCTET)
    assert r.status_code == 413 and world.proc.calls == [] and rows() == []


def test_chunked_body_within_the_cap_works(world):
    data = imgfix.png(32, 32)
    chunks = [data[i:i + 100] for i in range(0, len(data), 100)]
    r = world.a.post(f"/kaisen/builds/{world.bid}/uploads", params={"kind": "asset", "filename": "c.png"}, content=iter(chunks), headers=OCTET)
    assert r.status_code == 201 and rows()[0].orig_sha256 == hashlib.sha256(data).hexdigest()


def test_unauthenticated_oversized_chunked_body_is_never_read(world):
    """R8: 401 comes from a dependency, so the streaming body must not be consumed at all."""
    received = {"n": 0}
    sent = {"done": False}

    async def run():
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": f"/kaisen/builds/{world.bid}/uploads", "raw_path": b"", "root_path": "",
                 "query_string": b"kind=asset", "headers": [(b"content-type", b"application/octet-stream"), (b"host", b"testserver")],
                 "client": ("1.2.3.4", 1), "server": ("testserver", 80)}
        out = []

        async def receive():
            received["n"] += 1
            return {"type": "http.request", "body": b"0" * 65536, "more_body": True}

        async def send(msg):
            out.append(msg)
            if msg["type"] == "http.response.body":
                sent["done"] = True

        await fastapi_app(scope, receive, send)
        return out

    out = asyncio.run(run())
    assert out[0]["status"] == 401 and received["n"] == 0 and sent["done"]


def test_anonymous_and_cross_user_requests_are_refused(world):
    from fastapi.testclient import TestClient
    anon = TestClient(fastapi_app)
    base = f"/kaisen/builds/{world.bid}/uploads"
    assert up(world.a, world.bid, imgfix.png(8, 8)).status_code == 201
    uid = rows()[0].id
    for method, path in (("get", base), ("post", base + "?kind=asset"), ("put", f"{base}/{uid}"), ("patch", f"{base}/{uid}"),
                         ("delete", f"{base}/{uid}"), ("get", f"{base}/{uid}/content")):
        assert getattr(anon, method)(path).status_code == 401, (method, path)
    for c in (world.b, world.c):  # not members of a's personal project
        assert c.get(base).status_code == 404
        assert up(c, world.bid, imgfix.png()).status_code == 404
        assert c.put(f"{base}/{uid}", content=imgfix.png(), headers=OCTET).status_code == 404
        assert c.patch(f"{base}/{uid}", json={"description": "x"}).status_code == 404
        assert c.delete(f"{base}/{uid}").status_code == 404
        assert c.get(f"{base}/{uid}/content").status_code == 404
    assert len(rows()) == 1


def test_an_upload_id_from_another_build_is_a_404(world):
    other = world.a.post("/kaisen/builds", json={"project_id": world.pid, "name": "Other", "brief": "x"}).json()["id"]
    up(world.a, world.bid, imgfix.png(8, 8))
    uid = rows()[0].id
    base = f"/kaisen/builds/{other}/uploads/{uid}"
    assert world.a.get(base + "/content").status_code == 404
    assert world.a.delete(base).status_code == 404
    assert world.a.patch(base, json={"description": "x"}).status_code == 404
    assert world.a.put(base, content=imgfix.png(), headers=OCTET).status_code == 404
    assert len(rows()) == 1


def test_team_member_can_upload_and_read_read_only_key_cannot_write(world):
    shared = world.a.post("/projects", json={"name": "Team"}).json()["id"]
    inv = world.a.post(f"/projects/{shared}/invites", json={"role": "member"}).json()
    assert world.b.post(f"/invites/{inv['token']}/accept").status_code == 200
    world.admin.put(f"/admin/projects/{shared}/kaisen-uploads", json={"enabled": True})
    bid = world.a.post("/kaisen/builds", json={"project_id": shared, "name": "T", "brief": "x"}).json()["id"]
    assert up(world.b, bid, imgfix.png(8, 8)).status_code == 201
    key = world.b.post("/api/keys", json={"scope": "read"}).json()["key"]
    ro = type(world.b)(fastapi_app)
    ro.headers["Authorization"] = f"Bearer {key}"
    assert ro.get(f"/kaisen/builds/{bid}/uploads").status_code == 200
    r = up(ro, bid, imgfix.png(8, 8))
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    uid = rows()[0].id
    assert ro.delete(f"/kaisen/builds/{bid}/uploads/{uid}").status_code == 403
    assert len(rows()) == 1


def test_validation_errors(world):
    base = f"/kaisen/builds/{world.bid}/uploads"
    assert world.a.post(base, content=imgfix.png(), headers=OCTET).status_code == 422  # kind is required
    assert up(world.a, world.bid, imgfix.png(), kind="avatar").status_code == 422
    assert up(world.a, world.bid, imgfix.png(), description="d" * 3000).status_code == 422
    assert world.a.patch(f"{base}/1", json={"description": "d" * (config.KAISEN_UPLOAD_DESC_MAX + 1)}).status_code in (404, 422)
    assert up(world.a, world.bid, imgfix.png(8, 8)).status_code == 201
    uid = rows()[0].id
    assert world.a.patch(f"{base}/{uid}", json={"description": "d" * (config.KAISEN_UPLOAD_DESC_MAX + 1)}).status_code == 422
    assert world.a.patch(f"{base}/{uid}", json={"description": "x", "extra": 1}).status_code == 422
    assert world.a.get("/kaisen/builds/99999/uploads").status_code == 404
    assert world.a.get(f"{base}/abc/content").status_code == 422


# ---- caps --------------------------------------------------------------------------------------------------------------

def test_max_files_per_build(world, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_FILES", 2)
    assert up(world.a, world.bid, imgfix.png(8, 8)).status_code == 201
    assert up(world.a, world.bid, b"notes", kind="reference", filename="n.txt").status_code == 201
    r = up(world.a, world.bid, imgfix.png(9, 9))
    assert r.status_code == 403 and "already has 2 of 2 files" in r.json()["detail"] and len(rows()) == 2


def test_asset_byte_cap_per_build_counts_stored_size_and_replace_nets_the_old_size(world, monkeypatch):
    one = len(imgfix.png(40, 40))
    monkeypatch.setattr(config, "KAISEN_UPLOAD_BUILD_BYTES", one * 2 + 10)
    assert up(world.a, world.bid, imgfix.png(40, 40)).status_code == 201
    assert up(world.a, world.bid, imgfix.png(40, 40)).status_code == 201
    r = up(world.a, world.bid, imgfix.png(40, 40))
    assert r.status_code == 403 and "images can use at most" in r.json()["detail"]
    uid = rows()[0].id
    assert world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.png(40, 40), headers=OCTET).status_code == 200


def test_reference_byte_cap_per_build(world, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_REF_BYTES", 20)
    assert up(world.a, world.bid, b"a" * 15, kind="reference", filename="a.txt").status_code == 201
    r = up(world.a, world.bid, b"b" * 15, kind="reference", filename="b.txt")
    assert r.status_code == 403 and "reference files" in r.json()["detail"]


def test_project_user_and_platform_caps(world, monkeypatch):
    n = len(imgfix.png(30, 30))
    other = world.a.post("/kaisen/builds", json={"project_id": world.pid, "name": "Two", "brief": "x"}).json()["id"]
    monkeypatch.setattr(config, "KAISEN_UPLOAD_PROJECT_BYTES", n + 5)
    assert up(world.a, world.bid, imgfix.png(30, 30)).status_code == 201
    r = up(world.a, other, imgfix.png(30, 30))
    assert r.status_code == 403 and "project's uploads" in r.json()["detail"]
    monkeypatch.setattr(config, "KAISEN_UPLOAD_PROJECT_BYTES", 10**9)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_USER_BYTES", n + 5)
    r = up(world.a, other, imgfix.png(30, 30))
    assert r.status_code == 403 and "Your uploads" in r.json()["detail"]
    monkeypatch.setattr(config, "KAISEN_UPLOAD_USER_BYTES", 10**9)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_PLATFORM_BYTES", n + 5)
    r = up(world.a, other, imgfix.png(30, 30))
    assert r.status_code == 403 and "Uploads are full" in r.json()["detail"]
    assert len(rows()) == 1


def test_deleting_frees_quota(world, monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_FILES", 1)
    up(world.a, world.bid, imgfix.png(8, 8))
    assert up(world.a, world.bid, imgfix.png(9, 9)).status_code == 403
    world.a.delete(f"/kaisen/builds/{world.bid}/uploads/{rows()[0].id}")
    assert up(world.a, world.bid, imgfix.png(9, 9)).status_code == 201


# ---- rate limit ---------------------------------------------------------------------------------------------------------

def test_rate_limit_counts_rejected_attempts_and_sends_retry_after(world, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_RATE_MAX", 3)
    assert up(world.a, world.bid, b"not an image").status_code == 415  # rejected, still counts
    assert up(world.a, world.bid, b"nor this").status_code == 415
    assert up(world.a, world.bid, imgfix.png(8, 8)).status_code == 201
    r = up(world.a, world.bid, imgfix.png(9, 9))
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0 and len(rows()) == 1
    assert world.a.get(f"/kaisen/builds/{world.bid}/uploads").status_code == 200  # reads are not throttled


def test_rate_limit_is_checked_before_the_body_is_read_and_replace_counts(world, monkeypatch):
    up(world.a, world.bid, imgfix.png(8, 8))
    uid = rows()[0].id
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_RATE_MAX", 1)
    assert world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.png(9, 9), headers=OCTET).status_code == 200
    assert world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.png(9, 9), headers=OCTET).status_code == 429


# ---- replace / describe / delete -----------------------------------------------------------------------------------------

def test_replace_keeps_name_bumps_version_and_requires_same_type(world):
    r = up(world.a, world.bid, imgfix.png(10, 10), filename="logo.png", description="old words")
    uid = r.json()["id"]
    put = world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.png(20, 30), headers=OCTET)
    assert put.status_code == 200 and put.json()["version"] == 2 and put.json()["path"] == "uploads/logo.png"
    assert (put.json()["width"], put.json()["height"], put.json()["description"]) == (20, 30, "old words")
    jpg = world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.jpeg(), headers=OCTET)
    assert jpg.status_code == 415 and "same type (png)" in jpg.json()["detail"] and rows()[0].version == 2
    txt = world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=b"hello", headers=OCTET)
    assert txt.status_code == 415
    again = world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", params={"description": "new words"}, content=imgfix.png(5, 5), headers=OCTET)
    assert again.json()["description"] == "new words" and again.json()["version"] == 3
    assert len(audits("kaisen.upload_replace")) == 2


def test_replace_and_delete_wait_for_an_active_run(world):
    uid = up(world.a, world.bid, imgfix.png(8, 8)).json()["id"]
    s = db()
    s.add(KaisenRun(build_id=world.bid, kind="build", status="running", messages=[]))
    s.commit()
    r = world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.png(9, 9), headers=OCTET)
    assert r.status_code == 409 and "Wait for the current run" in r.json()["detail"]
    assert world.a.delete(f"/kaisen/builds/{world.bid}/uploads/{uid}").status_code == 409
    assert len(rows()) == 1
    assert world.a.patch(f"/kaisen/builds/{world.bid}/uploads/{uid}", json={"description": "ok"}).status_code == 200
    assert up(world.a, world.bid, imgfix.png(7, 7)).status_code == 201  # adding a new file during a run is allowed


def test_describe_is_audited_and_cleaned(world):
    uid = up(world.a, world.bid, imgfix.png(8, 8)).json()["id"]
    r = world.a.patch(f"/kaisen/builds/{world.bid}/uploads/{uid}", json={"description": "line1\nline2 <script>"})
    assert r.status_code == 200 and r.json()["description"] == "line1 line2 script"
    assert len(audits("kaisen.upload_describe")) == 1


def test_delete_removes_row_and_bundles_and_is_audited(world):
    uid = up(world.a, world.bid, imgfix.png(8, 8)).json()["id"]
    s = db()
    s.add(KaisenBundle(build_id=world.bid, token_hash="h" * 64, sha256="s" * 64, data=b"bundle"))
    s.commit()
    assert world.a.delete(f"/kaisen/builds/{world.bid}/uploads/{uid}").status_code == 204
    s.expire_all()
    assert rows() == [] and s.query(KaisenBundle).count() == 0
    assert len(audits("kaisen.upload_delete")) == 1
    assert world.a.delete(f"/kaisen/builds/{world.bid}/uploads/{uid}").status_code == 404


def test_suspended_project_blocks_adding_but_members_can_still_delete(world):
    uid = up(world.a, world.bid, imgfix.png(8, 8)).json()["id"]
    assert world.admin.post(f"/admin/projects/{world.pid}/suspend", json={"reason": "abuse report"}).status_code == 200
    assert up(world.a, world.bid, imgfix.png(9, 9)).status_code == 403
    assert world.a.put(f"/kaisen/builds/{world.bid}/uploads/{uid}", content=imgfix.png(9, 9), headers=OCTET).status_code == 403
    assert world.a.patch(f"/kaisen/builds/{world.bid}/uploads/{uid}", json={"description": "x"}).status_code == 403
    assert world.a.get(f"/kaisen/builds/{world.bid}/uploads").status_code == 200
    assert world.a.delete(f"/kaisen/builds/{world.bid}/uploads/{uid}").status_code == 204


def test_deleting_a_build_purges_its_uploads(world):
    up(world.a, world.bid, imgfix.png(8, 8))
    up(world.a, world.bid, b"notes", kind="reference", filename="n.txt")
    assert world.a.delete(f"/kaisen/builds/{world.bid}").status_code == 204
    assert rows() == []


def test_orphan_sweep_removes_uploads_of_long_deleted_builds(world):
    from datetime import datetime, timedelta
    from app.kaisen.models import KaisenBuild
    up(world.a, world.bid, imgfix.png(8, 8))
    s = db()
    s.add(KaisenAsset(build_id=424242, kind="asset", path="uploads/x.png", mime="image/png", ext="png", size=1, sha256="a" * 64,
                      orig_sha256="b" * 64, data=b"x"))  # its build row is gone
    s.commit()
    assert ku.sweep_orphans(s) == 1 and len(rows()) == 1
    b = s.get(KaisenBuild, world.bid)
    b.deleted_at = datetime.utcnow() - timedelta(days=config.KAISEN_UPLOAD_ORPHAN_DAYS + 1)
    s.commit()
    assert ku.sweep_orphans(s) == 1 and rows() == []


# ---- serving to the owner --------------------------------------------------------------------------------------------------

def test_content_route_headers_for_raster_and_svg(world):
    png_id = up(world.a, world.bid, imgfix.png(8, 8), filename="Pic.png").json()["id"]
    svg_id = up(world.a, world.bid, SVG, filename="Icon.svg").json()["id"]
    p = world.a.get(f"/kaisen/builds/{world.bid}/uploads/{png_id}/content")
    assert p.headers["content-type"] == "image/png" and p.headers["x-content-type-options"] == "nosniff"
    assert p.headers["content-security-policy"] == "default-src 'none'; sandbox" and "no-store" in p.headers["cache-control"]
    assert p.headers["etag"] == f'"{rows()[0].sha256}"' and p.headers["content-disposition"] == 'inline; filename="pic.png"'
    s = world.a.get(f"/kaisen/builds/{world.bid}/uploads/{svg_id}/content")
    assert s.headers["content-type"] == "image/svg+xml" and s.headers["x-content-type-options"] == "nosniff"
    assert s.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
    assert b"script" not in s.content


def test_session_cookie_is_host_only_so_sibling_preview_origins_cannot_read_it(make_client):
    c = make_client()
    r = c.post("/auth/register", json={"email": "cookie@example.com", "password": "correct-horse-battery", "accept_terms": True})
    cookie = r.headers["set-cookie"].lower()
    assert "domain=" not in cookie and "httponly" in cookie


def test_listing_never_selects_the_blob_column(world):
    up(world.a, world.bid, imgfix.png(8, 8))
    up(world.a, world.bid, imgfix.png(9, 9))
    seen = []
    engine = db().get_bind()

    def spy(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        assert world.a.get(f"/kaisen/builds/{world.bid}/uploads").status_code == 200
        assert world.a.get(f"/kaisen/builds/{world.bid}").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    asset_selects = [s for s in seen if "FROM kaisen_assets" in s and s.lstrip().upper().startswith("SELECT")]
    assert asset_selects and all("kaisen_assets.data" not in s for s in asset_selects)


# ---- admin: search, takedown, blocklist, summary -----------------------------------------------------------------------

def _bundle(bid, token="tok-not-a-secret"):
    from app.kaisen.deploy import token_hash
    s = db()
    s.add(KaisenBundle(build_id=bid, token_hash=token_hash(token), sha256="e" * 64, data=b"old bundle bytes"))
    s.commit()
    return token


def test_takedown_deletes_bytes_bundles_blocklists_both_hashes_and_is_audited(world):
    src = imgfix.png(30, 30, color=(9, 9, 9))
    uid = up(world.a, world.bid, src).json()["id"]
    token = _bundle(world.bid)
    assert world.a.get(f"/kaisen/bundles/{token}.tar.gz").status_code == 200
    stored, orig = rows()[0].sha256, rows()[0].orig_sha256
    stored_bytes = rows()[0].data
    r = world.admin.delete(f"/admin/kaisen/builds/{world.bid}/uploads/{uid}", params={"reason": "abuse report 17"})
    assert r.status_code == 200 and r.json()["taken_down"] is True and r.json()["sha256"] == stored
    assert rows() == [] and db().query(KaisenBundle).count() == 0
    assert world.a.get(f"/kaisen/bundles/{token}.tar.gz").status_code == 404  # the old public bundle URL is dead
    assert {b.sha256 for b in db().query(KaisenUploadBlock).all()} == {stored, orig}
    [a] = audits("kaisen.upload_takedown")
    assert stored[:12] in a.detail and "abuse report 17" in a.detail and a.actor_email == "admin@example.com"
    again = up(world.a, world.bid, src)  # the same source file
    assert again.status_code == 422 and again.json()["detail"] == ku.BLOCKED
    other = up(world.a, world.bid, stored_bytes)  # the re-encoded bytes (hash of the stored form)
    assert other.status_code == 422 and other.json()["detail"] == ku.BLOCKED
    assert up(world.a, world.bid, imgfix.png(31, 31)).status_code == 201  # exact-hash only
    assert any(x.detail == "reason=blocklist" for x in audits("kaisen.upload_rejected"))


def test_takedown_can_suspend_the_preview_app_and_the_next_bundle_has_no_such_bytes(world):
    from app.kaisen.models import KaisenFile
    from app.kaisen import deploy as deploy_mod
    s = db()
    s.add(KaisenFile(build_id=world.bid, path="kaisen.json", content='{"runtime":"static"}', size=20))
    s.add(KaisenFile(build_id=world.bid, path="public/index.html", content="<img src=/uploads/logo.png>", size=27))
    s.commit()
    uid = up(world.a, world.bid, imgfix.png(8, 8)).json()["id"]
    b = s.get(__import__("app.kaisen.models", fromlist=["KaisenBuild"]).KaisenBuild, world.bid)
    url, sha, _ = deploy_mod.make_bundle(s, b)
    s.commit()
    assert world.a.get(url.split("picopaas.xyz")[-1] if "picopaas.xyz" in url else url[url.index("/kaisen"):]).status_code == 200
    r = world.admin.delete(f"/admin/kaisen/builds/{world.bid}/uploads/{uid}", params={"reason": "csam-free test", "suspend_app": "true"})
    assert r.status_code == 200 and r.json()["app_suspended"] is False and r.json()["needs_redeploy"] is False  # no live app yet
    s.expire_all()
    url2, sha2, _ = deploy_mod.make_bundle(s, s.get(__import__("app.kaisen.models", fromlist=["KaisenBuild"]).KaisenBuild, world.bid))
    assert sha2 != sha
    with tarfile.open(fileobj=io.BytesIO(s.query(KaisenBundle).one().data), mode="r:gz") as t:
        assert not [n for n in t.getnames() if "uploads/" in n]


def test_takedown_suspends_a_live_preview_when_asked(world, make_client):
    from app.kaisen.models import KaisenFile, KaisenBuild
    s = db()
    s.add(KaisenFile(build_id=world.bid, path="kaisen.json", content='{"runtime":"static"}', size=20))
    s.add(KaisenFile(build_id=world.bid, path="public/index.html", content="<h1>x</h1>", size=10))
    s.commit()
    from app import config as cfg
    cfg.KAISEN_RUNTIME_IMAGE = "ghcr.io/example/kaisen-runtime:1"
    try:
        uid = up(world.a, world.bid, imgfix.png(8, 8)).json()["id"]
        dep = world.a.post(f"/kaisen/builds/{world.bid}/deploy")
        assert dep.status_code == 200, dep.text
        r = world.admin.delete(f"/admin/kaisen/builds/{world.bid}/uploads/{uid}", params={"reason": "report", "suspend_app": "true"})
        assert r.json()["app_suspended"] is True and r.json()["needs_redeploy"] is True
        s.expire_all()
        app_row = s.get(models.App, s.get(KaisenBuild, world.bid).app_id)
        assert app_row.status == "suspended" and "taken down" in app_row.suspended_reason
    finally:
        cfg.KAISEN_RUNTIME_IMAGE = ""


def test_takedown_and_search_are_admin_only(world):
    uid = up(world.a, world.bid, imgfix.png(8, 8)).json()["id"]
    sha = rows()[0].sha256
    from fastapi.testclient import TestClient
    anon = TestClient(fastapi_app)
    for c in (world.a, world.c):  # owner and outsider are not platform admins
        assert c.delete(f"/admin/kaisen/builds/{world.bid}/uploads/{uid}", params={"reason": "x"}).status_code == 403
        assert c.get("/admin/kaisen/uploads", params={"sha256": sha}).status_code == 403
        assert c.get("/admin/kaisen/uploads/summary").status_code == 403
    assert anon.delete(f"/admin/kaisen/builds/{world.bid}/uploads/{uid}", params={"reason": "x"}).status_code == 401
    assert anon.get("/admin/kaisen/uploads").status_code == 401
    assert world.admin.delete(f"/admin/kaisen/builds/{world.bid}/uploads/{uid}").status_code == 422  # reason is required
    assert world.admin.delete(f"/admin/kaisen/builds/{world.bid}/uploads/9999", params={"reason": "x"}).status_code == 404
    assert len(rows()) == 1


def test_admin_search_by_stored_or_original_hash_across_builds(world):
    src = imgfix.png(12, 12)
    b2 = world.a.post("/kaisen/builds", json={"project_id": world.pid, "name": "Two", "brief": "x"}).json()["id"]
    up(world.a, world.bid, src)
    up(world.a, b2, src)
    up(world.a, b2, imgfix.png(13, 13))
    stored = rows()[0].sha256
    by_stored = world.admin.get("/admin/kaisen/uploads", params={"sha256": stored.upper()}).json()
    by_orig = world.admin.get("/admin/kaisen/uploads", params={"sha256": hashlib.sha256(src).hexdigest()}).json()
    assert {r["build_id"] for r in by_stored} == {world.bid, b2} == {r["build_id"] for r in by_orig}
    assert "data" not in by_stored[0] and by_stored[0]["created_by_email"] == "a@example.com" and by_stored[0]["project_id"] == world.pid
    assert len(world.admin.get("/admin/kaisen/uploads", params={"build_id": b2}).json()) == 2
    assert world.admin.get("/admin/kaisen/uploads", params={"sha256": "xyz"}).status_code == 422
    assert world.admin.get("/admin/kaisen/uploads", params={"sha256": "0" * 64}).json() == []


def test_admin_summary_warns_at_eighty_percent(world, monkeypatch):
    up(world.a, world.bid, imgfix.png(30, 30))
    n = rows()[0].size
    s = world.admin.get("/admin/kaisen/uploads/summary").json()
    assert s["platform_bytes"] == n and s["files"] == 1 and s["warn"] is False and s["blocklist_entries"] == 0
    monkeypatch.setattr(config, "KAISEN_UPLOAD_PLATFORM_BYTES", int(n / 0.85))
    assert world.admin.get("/admin/kaisen/uploads/summary").json()["warn"] is True


# ---- module-level helpers -------------------------------------------------------------------------------------------------

def test_reserved_paths_helper_is_case_insensitive():
    from app.kaisen.paths import is_reserved_upload_path as r
    assert all(r(p) for p in ("uploads/a.png", "Uploads/a.png", "UPLOADS/x", "references/a.css", "References/a", ".kaisen-uploads.json", "uploads"))
    assert not any(r(p) for p in ("public/uploads/a.png", "upload/a.png", "src/app.py"))


def test_hourly_sweep_job_is_leader_only_and_survives_errors(world, monkeypatch):
    from app import scheduler
    s = db()
    s.add(KaisenAsset(build_id=424242, kind="asset", path="uploads/x.png", mime="image/png", ext="png", size=1, sha256="a" * 64,
                      orig_sha256="b" * 64, data=b"x"))
    s.commit()
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: s)
    monkeypatch.setattr(s, "close", lambda: None)
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: False)
    scheduler.kaisen_upload_sweep_job()
    assert len(rows()) == 1
    monkeypatch.setattr(scheduler, "try_acquire_leader", lambda: True)
    scheduler.kaisen_upload_sweep_job()
    assert rows() == []

    def boom(_):
        raise RuntimeError("db down")
    monkeypatch.setattr(ku, "sweep_orphans", boom)
    scheduler.kaisen_upload_sweep_job()  # logged, not raised


def test_http_processor_factory_builds_the_real_client_once():
    from app.providers.imgproc import HttpImageProcessor
    from app.routers import kaisen_uploads
    kaisen_uploads._processor = None
    first = kaisen_uploads.get_image_processor()
    assert isinstance(first, HttpImageProcessor) and kaisen_uploads.get_image_processor() is first
    assert first.base_url == config.KAISEN_IMGPROC_URL
    kaisen_uploads._processor = None
