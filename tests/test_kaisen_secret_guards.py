"""Self-harm guards (docs/design/kaisen.md 16.10) and refusal messages that state the limit and the file's value."""
import importlib.util
from pathlib import Path

import pytest

from app import config
from app.imgproc import core
from app.kaisen import builder, paths, secretscan
from app.kaisen.models import KaisenFile
from app.kaisen.paths import ToolError
from app.providers.base import ImageProcessError
from tests import imgfix
from tests.kaisen_helpers import db
from tests.test_kaisen_uploads_api import proc, up, world  # noqa: F401  (fixtures)

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"


# Credential-shaped strings are assembled at runtime so no literal looks like a secret to a scanner.
def _aws():
    return "AK" + "IA" + "ABCDEFGHIJKLMNOP"


def _pem():
    return "-----BEGIN " + "RSA PRIVATE" + " KEY-----"


def _slack():
    return "xo" + "xb-" + "1234567890-abcdefghij"


def _stripe():
    return "sk" + "_live_" + "abcdefghijklmnop1234"


def _gh():
    return "gh" + "p_" + "a1b2c3d4e5f6g7h8i9j0k1l2"


def _assign():
    return 'api_key = "' + "q8Zr4TnV7xL2mPw9Yd3K" + '"'


# ---- messages state limit and value -----------------------------------------------------------------------------------

def _err(data, hint="raster"):
    with pytest.raises(ImageProcessError) as e:
        core.process(data, hint)
    return e.value.message


def test_pixel_refusal_states_megapixels_dimensions_and_limit():
    msg = _err(imgfix.png(4000, 2350, mode="L"))
    assert "9.4 megapixels (4000 × 2350)" in msg and "most allowed is 8 megapixels" in msg


def test_edge_refusal_states_size_and_limit():
    msg = _err(imgfix.png(config.KAISEN_UPLOAD_MAX_EDGE_IN + 1, 2, mode="L"))
    assert f"{config.KAISEN_UPLOAD_MAX_EDGE_IN + 1} × 2" in msg and f"at most {config.KAISEN_UPLOAD_MAX_EDGE_IN} pixels" in msg


def test_bomb_error_path_still_reports_the_numbers(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_PIXELS", 1000)
    msg = _err(imgfix.png(100, 100, mode="L"))
    assert "100 × 100" in msg and "most allowed" in msg


def test_svg_size_elements_depth_and_images_state_limits(monkeypatch):
    big = b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"<g/>" * 200_000 + b"</svg>"
    m = _err(big, "svg")
    assert "KB" in m and f"{config.KAISEN_UPLOAD_SVG_BYTES // 1024} KB" in m
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_ELEMENTS", 3)
    assert "more than 3 elements" in _err(b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"<g/>" * 5 + b"</svg>", "svg")
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_ELEMENTS", 100)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_DEPTH", 2)
    deep = b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"<g>" * 5 + b"</g>" * 5 + b"</svg>"
    assert "more than 2 levels" in _err(deep, "svg")


def test_svg_embedded_image_count_states_count_and_limit(monkeypatch):
    import base64
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_IMAGES", 1)
    uri = "data:image/png;base64," + base64.b64encode(imgfix.png(4, 4)).decode()
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'>"
           + f"<image href='{uri}' width='1' height='1'/>" * 3 + "</svg>").encode()
    msg = _err(svg, "svg")
    assert "embeds 3 pictures" in msg and "most allowed is 1" in msg


def test_http_422_carries_the_numbers_end_to_end(world):  # noqa: F811
    r = up(world.a, world.bid, imgfix.png(4000, 2350, mode="L"))
    assert r.status_code == 422 and "9.4 megapixels (4000 × 2350)" in r.json()["detail"]
    assert "8 megapixels" in r.json()["detail"]


def test_body_too_large_states_size_and_limit(world):  # noqa: F811
    r = up(world.a, world.bid, b"\x89PNG\r\n\x1a\n" + b"0" * (config.KAISEN_UPLOAD_MAX_BYTES_IN + 1))
    d = r.json()["detail"]
    assert r.status_code == 413 and "4.0 MB" in d and "most allowed is 4 MB" in d


def test_type_refusals_list_allowed_types(world):  # noqa: F811
    d = up(world.a, world.bid, b"%PDF-1.4 hello").json()["detail"]
    assert "PNG, JPEG, WebP, SVG" in d
    d = up(world.a, world.bid, b"\x89PNG\r\n\x1a\nxx", kind="reference", filename="a.png").json()["detail"]
    assert "CSS, HTML" in d and "Markdown" in d


def test_quota_refusals_show_used_and_limit(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_FILES", 1)
    assert up(world.a, world.bid, b"a{}", kind="reference", filename="a.css").status_code == 201
    d = up(world.a, world.bid, b"b{}", kind="reference", filename="b.css").json()["detail"]
    assert "1 of 1 files" in d
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_FILES", 20)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_REF_BYTES", 10)
    d = up(world.a, world.bid, b"c" * 20, kind="reference", filename="c.txt").json()["detail"]
    assert "can use at most" in d and "MB are used" in d and "this file is" in d


def test_reference_size_refusal_states_size(world):  # noqa: F811
    d = up(world.a, world.bid, b"x" * (config.KAISEN_UPLOAD_TEXT_BYTES + 1024), kind="reference", filename="a.txt").json()["detail"]
    assert "101.0 KB" in d and f"at most {config.KAISEN_UPLOAD_TEXT_BYTES // 1024} KB" in d


# ---- uploads: credentials ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("secret,kind", [(_pem(), "private key"), (_aws(), "AWS"), (_slack(), "Slack"),
                                         (_stripe(), "Stripe"), (_gh(), "GitHub"), (_assign(), "assignment")])
def test_reference_with_credential_is_refused_without_echo(world, secret, kind):  # noqa: F811
    text = f"body {{}}\n/* notes */\n{secret}\n"
    r = up(world.a, world.bid, text.encode(), kind="reference", filename="notes.txt")
    d = r.json()["detail"]
    assert r.status_code == 422 and "(line 3:" in d and kind in d and "Build secrets" in d
    assert secret not in d and "q8Zr4TnV7xL2mPw9Yd3K" not in d


def test_svg_with_credential_in_text_is_refused(world):  # noqa: F811
    svg = f"<svg xmlns='http://www.w3.org/2000/svg'><!-- {_aws()} --><rect width='1' height='1'/></svg>".encode()
    r = up(world.a, world.bid, svg)
    assert r.status_code == 422 and "line 1" in r.json()["detail"] and _aws() not in r.json()["detail"]


def test_placeholder_examples_are_not_flagged():
    assert secretscan.find("API_KEY=your-api-key-here\npassword = 'changeme1234567890abc'", code=True) is None
    assert secretscan.find("AKIAIOSFODNN7EXAMPLE") is None


# ---- builder tools ------------------------------------------------------------------------------------------------------

@pytest.fixture()
def ws(make_client):
    from tests.test_kaisen_units import _build
    s = db()
    yield builder.Workspace(s, _build(s).id)
    s.close()


@pytest.mark.parametrize("path", [".env", ".env.production", "app/.env.local", "server.pem", "tls/site.key", "id_rsa",
                                  "keys/id_ed25519", "a.p12", "x.pfx", "prod.env"])
def test_write_refuses_env_and_key_files(ws, path):
    with pytest.raises(ToolError, match="refused"):
        ws.write(path, "x")
    assert ws.s.query(KaisenFile).filter_by(path=path).count() == 0


@pytest.mark.parametrize("path", ["public/.htaccess", "public/data.sqlite", "public/backup.sql", "public/index.html.bak",
                                  "public/app.py", "public/kaisen.json", "public/debug.log", "public/secrets.json"])
def test_write_refuses_unservable_names_under_public(ws, path):
    with pytest.raises(ToolError, match="never served"):
        ws.write(path, "x")


@pytest.mark.parametrize("path", ["app.py", "Dockerfile", ".gitignore", "public/index.html", "public/config.json", "tests/t.py"])
def test_write_allows_normal_files(ws, path):
    assert ws.write(path, "x = 1\n").startswith("wrote")


@pytest.mark.parametrize("secret", [_pem(), _aws(), _slack(), _stripe(), _gh(), _assign()])
def test_write_and_edit_refuse_credentials_and_do_not_echo(ws, secret):
    with pytest.raises(ToolError, match="Build secrets") as e:
        ws.write("app.py", f"x = 1\n{secret}\n")
    assert secret not in str(e.value) and "line 2" in str(e.value)
    ws.write("app.py", "x = 1\n")
    with pytest.raises(ToolError, match="Build secrets"):
        ws.edit("app.py", "x = 1", secret)
    assert ws.read("app.py") == "x = 1\n"


def test_normal_code_is_not_flagged(ws):
    code = "import os\nAPI_KEY = os.environ['API_KEY']\npassword = secrets.token_hex(16)\ntoken = request.headers['x']\n"
    assert ws.write("app.py", code).startswith("wrote")


def test_prompt_states_the_rules():
    t = open("app/kaisen/prompts/builder.md").read()
    assert "Build secrets" in t and "dotfiles" in t and ".env*" in t


# ---- preview bundle ----------------------------------------------------------------------------------------------------

def test_bundle_drops_env_and_key_files(world):  # noqa: F811
    import io
    import tarfile

    from app.kaisen import deploy as deploy_mod
    from app.kaisen.models import KaisenBundle
    s = db()
    for p in ("kaisen.json", "public/index.html", ".env", "keys/server.pem"):
        s.add(KaisenFile(build_id=world.bid, path=p, content="{}", size=2))
    s.commit()
    from app.kaisen.models import KaisenBuild
    deploy_mod.make_bundle(s, s.get(KaisenBuild, world.bid))
    s.commit()
    data = s.query(KaisenBundle).filter_by(build_id=world.bid).one().data
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        names = {m.name.split("/", 1)[1] for m in t.getmembers() if m.isfile()}
    assert names == {"kaisen.json", "public/index.html"}


# ---- python and runtime lists agree --------------------------------------------------------------------------------------

def test_runtime_and_builder_lists_agree():
    spec = importlib.util.spec_from_file_location("kaisen_boot_lists", BOOT)
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    assert set(paths.PRIVATE_KEY_EXT) <= boot.BLOCK_EXT
    assert set(paths.SERVED_BLOCK_EXT) <= boot.BLOCK_EXT
    assert set(paths.SERVED_BLOCK_NAMES) == boot.BLOCK_NAMES
    assert set(paths.PRIVATE_KEY_PREFIX) <= set(boot.BLOCK_PREFIX)
    assert set(paths.SERVED_BLOCK_PREFIX) <= set(boot.BLOCK_PREFIX)
