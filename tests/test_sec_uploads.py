"""Security review 2026-11-03 (K76, K77): CSS string-URL functions in SVG, and admin upload search is web-UI only."""
import pytest

from app.imgproc import core
from app.main import app as fastapi_app
from app.providers.base import ImageProcessError
from tests.test_kaisen_uploads_api import world, proc, up  # noqa: F401 - fixtures
from tests import imgfix

NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'
HOST = "https://tracker.invalid/p.png"


def clean(inner):
    return core.process(f'<svg {NS} width="10" height="10">{inner}</svg>'.encode(), "svg").data.decode()


@pytest.mark.parametrize("style", [
    f"background:image-set('{HOST}' 1x)",
    f"cursor:-webkit-image-set('{HOST}' 1x), auto",
    f"background:src('{HOST}')",
    f"background:image('{HOST}')",
    f"background:cross-fade(url(#a), '{HOST}')",
    f"background:element('{HOST}')",
    "background:paint(x)",
    f"background: IMAGE-SET ( '{HOST}' 1x)",
])
def test_css_functions_that_take_a_plain_string_url_are_dropped(style):
    out = clean(f'<rect style="{style}"/><style>rect{{{style}}}</style>')
    assert "tracker" not in out and "style=" not in out and "<style" not in out


def test_normal_css_still_survives():
    out = clean('<style>rect:nth-child(2){fill:rgb(1,2,3);transform:rotate(5deg)}</style><rect fill="url(#g)" style="stroke:hsl(1,2%,3%)"/>')
    assert "rotate(5deg)" in out and "url(#g)" in out and "hsl(" in out


@pytest.mark.parametrize("body", [
    '<a xlink:href="https://x.invalid/"><rect/></a>',
    '<foreignObject><iframe xmlns="http://www.w3.org/1999/xhtml" src="https://x.invalid"/></foreignObject>',
    '<use href="https://x.invalid/a.svg#b"/>',
    '<image href="data:text/html;base64,PHNjcmlwdD4="/>',
    '<image href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="/>',
    '<rect><animate attributeName="fill" values="javascript:x"/></rect>',
    '<script xmlns="http://www.w3.org/2000/svg">x</script>',
    '<style>@import "https://x.invalid/a.css";</style>',
])
def test_known_active_content_vectors_are_gone(body):
    out = clean(body)
    for bad in ("x.invalid", "script", "iframe", "animate", "foreignObject", "data:text", "svg+xml", "@import"):
        assert bad not in out.split("xmlns:xlink=")[-1]


def test_doctype_and_entities_are_refused():
    for doc in (b'<!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
                b'<!DOCTYPE svg SYSTEM "http://x.invalid/a.dtd"><svg xmlns="http://www.w3.org/2000/svg"/>'):
        with pytest.raises(ImageProcessError):
            core.process(doc, "svg")


def test_admin_upload_search_is_web_ui_only_for_api_keys(world):  # noqa: F811
    assert up(world.a, world.bid, imgfix.png()).status_code == 201
    assert world.admin.get("/admin/kaisen/uploads").status_code == 200
    key = world.admin.post("/api/keys", json={"scope": "read"}).json()["key"]
    ro = type(world.admin)(fastapi_app)
    ro.headers["Authorization"] = f"Bearer {key}"
    assert ro.get("/admin/kaisen/uploads").status_code == 403
