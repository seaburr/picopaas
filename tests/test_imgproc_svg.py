"""SVG sanitizer corpus (Chas 2026-11-03: SVG is allowed only after strict sanitizing in the image unit)."""
import pytest
from defusedxml.ElementTree import fromstring

from app import config
from app.imgproc import core, svg
from app.providers.base import ImageProcessError

NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


def wrap(inner, attrs="", ns=NS):
    return f'<svg {ns} width="100" height="50" {attrs}>{inner}</svg>'.encode()


def clean(inner, attrs=""):
    out = core.process(wrap(inner, attrs), "svg").data.decode()
    fromstring(out.encode(), forbid_dtd=True, forbid_entities=True, forbid_external=True)  # the output parses strictly
    return out


def refuses(data, code):
    with pytest.raises(ImageProcessError) as ei:
        core.process(data, "svg")
    assert ei.value.code == code
    return ei.value


def test_a_plain_logo_survives_with_its_shapes_and_size():
    res = core.process(wrap('<defs><linearGradient id="g"><stop offset="0" stop-color="#f00"/><stop offset="1" stop-color="#00f"/>'
                            '</linearGradient></defs><rect x="1" y="2" width="30" height="20" fill="url(#g)" rx="3"/>'
                            '<circle cx="5" cy="5" r="4" fill="#0a0"/><path d="M0 0L10 10z" stroke="black"/>'
                            '<text x="1" y="9" font-size="8">Hello &amp; welcome</text>'), "svg")
    out = res.data.decode()
    assert res.mime == "image/svg+xml" and (res.width, res.height) == (100, 50)
    for needle in ('<rect ', '<circle ', '<path ', 'url(#g)', '<linearGradient id="g">', "Hello &amp; welcome"):
        assert needle in out
    assert out.startswith("<svg ") and 'xmlns="http://www.w3.org/2000/svg"' in out


def test_viewbox_gives_the_size_when_width_and_height_are_missing():
    res = core.process(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120.4 60"><rect width="1" height="1"/></svg>', "svg")
    assert (res.width, res.height) == (120, 60)


@pytest.mark.parametrize("payload", [
    '<script>alert(1)</script>',
    '<script type="text/javascript"><![CDATA[alert(document.cookie)]]></script>',
    '<g><script>alert(1)</script><rect width="1" height="1"/></g>',
])
def test_script_elements_are_dropped(payload):
    out = clean(payload)
    assert "script" not in out.lower() and "alert" not in out


@pytest.mark.parametrize("attr", ["onload", "onclick", "onmouseover", "onerror", "onfocus", "onbegin", "ONLOAD"])
def test_event_handler_attributes_are_dropped(attr):
    out = clean(f'<rect width="1" height="1" {attr}="alert(1)"/>', f'{attr}="alert(2)"')
    assert "alert" not in out and attr.lower() not in out.lower()


@pytest.mark.parametrize("href", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)",
                                  "data:text/html,<script>alert(1)</script>", "https://evil.example/x.svg", "//evil.example/x",
                                  "file:///etc/passwd", "vbscript:msgbox(1)", "other.svg#a"])
def test_dangerous_hrefs_are_dropped_and_use_without_target_goes(href):
    href = href.replace("<", "&lt;")
    out = clean(f'<use href="{href}"/><use xlink:href="{href}"/><image href="{href}" width="1" height="1"/>'
                f'<linearGradient id="q" xlink:href="{href}"/>')
    assert "javascript" not in out.lower() and "evil" not in out and "file:" not in out and "<use" not in out
    assert "<image" not in out and 'href=' not in out


def test_anchor_elements_are_dropped():
    out = clean('<a xlink:href="javascript:alert(1)"><rect width="1" height="1"/></a><a href="https://evil.example"><text>x</text></a>')
    assert "<a" not in out and "evil" not in out and "javascript" not in out


def test_foreign_object_is_dropped_with_its_html():
    out = clean('<foreignObject width="10" height="10"><div xmlns="http://www.w3.org/1999/xhtml"><iframe src="https://evil.example"/>'
                '<script>alert(1)</script></div></foreignObject><rect width="1" height="1"/>')
    assert "foreignObject" not in out and "iframe" not in out and "evil" not in out and "<rect" in out


def test_other_namespace_elements_are_dropped():
    out = clean('<x:evil xmlns:x="http://evil.example/ns"><rect width="1" height="1"/></x:evil><html xmlns="http://www.w3.org/1999/xhtml"><b>x</b></html>')
    assert "evil" not in out and "<b" not in out and "html" not in out.lower().replace("xhtml", "")


@pytest.mark.parametrize("anim", [
    '<animate attributeName="href" values="javascript:alert(1)" begin="0s"/>',
    '<set attributeName="href" to="javascript:alert(1)"/>',
    '<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="1s"/>',
    '<animateMotion path="M0 0L9 9" dur="1s"/>',
])
def test_all_animation_elements_are_dropped(anim):
    out = clean(f'<rect width="1" height="1">{anim}</rect>')
    assert "animate" not in out and "<set" not in out and "javascript" not in out


@pytest.mark.parametrize("css", [
    "fill:red;background:url(https://evil.example/x.png)",
    "fill:url(javascript:alert(1))",
    "background:url(data:image/png;base64,AAAA)",
    "fill:red;behavior:url(#default#time2)x",
    "width:expression(alert(1))",
    "@import 'https://evil.example/x.css';",
    "-moz-binding:url(http://evil.example/x.xml#b)",
    "fill:\\75rl(https://evil.example)",
    "color:red;/*x*/background:u/**/rl(x)",
])
def test_style_attributes_with_fetching_or_executing_css_are_dropped(css):
    out = clean(f'<rect width="1" height="1" style="{css}"/>')
    assert 'style=' not in out and "evil" not in out and "expression" not in out


@pytest.mark.parametrize("css", [
    "@import url(https://evil.example/x.css); rect{fill:red}",
    "@import 'https://evil.example/x.css';",
    "rect{fill:url(https://evil.example/a.svg#f)}",
    "rect{background:url('//evil.example/x')}",
    "rect{width:expression(alert(1))}",
    "@font-face{font-family:x;src:url(https://evil.example/f.woff)}",
    "rect{fill:\\72ed}",
])
def test_style_elements_with_dangerous_css_are_dropped(css):
    out = clean(f'<style>{css}</style><rect width="1" height="1"/>')
    assert "<style" not in out and "evil" not in out and "<rect" in out


def test_harmless_css_and_local_url_references_are_kept():
    out = clean('<style>rect{fill:url(#g);stroke:#333}.a{opacity:.5}</style><rect class="a" style="fill:url(#g);stroke:blue" width="1" height="1"/>')
    assert "<style>" in out and "stroke:#333" in out and 'style="fill:url(#g);stroke:blue"' in out


def test_use_with_a_local_fragment_is_kept_but_not_chains_or_missing_targets():
    ok = clean('<defs><rect id="a" width="2" height="2"/></defs><use href="#a"/><use xlink:href="#a" x="3"/>')
    assert ok.count("<use") == 2
    gone = clean('<use href="#missing"/>')
    assert "<use" not in gone
    chain = clean('<g id="a"><use href="#b"/></g><g id="b"><rect width="1" height="1"/></g><use href="#a"/>')
    assert chain.count("<use") == 1 and '<use href="#a"/>' not in chain  # only the inner use (target holds no use) is kept


def test_use_bomb_by_self_reference_is_dropped():
    out = clean('<g id="a"><use href="#a"/></g>')
    assert "<use" not in out


def _png_b64(size=(4, 3), exif=True, fmt="PNG"):
    import base64
    import io

    from PIL import Image
    img = Image.new("RGB", size, (200, 10, 10))
    buf = io.BytesIO()
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[0x010E] = "secret-description-marker"
        kw["exif"] = ex
    img.save(buf, fmt, **kw)
    return base64.b64encode(buf.getvalue()).decode(), buf.getvalue()


def test_image_element_accepts_only_raster_data_uris():
    b64, _ = _png_b64()
    out = clean(f'<image width="1" height="1" href="data:image/png;base64,{b64}"/>'
                '<image width="1" height="1" href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="/>'
                '<image width="1" height="1" href="data:text/html;base64,PHNjcmlwdD4="/>')
    assert out.count("<image") == 1 and "data:image/png;base64," in out and "svg+xml" not in out and "text/html" not in out


def test_embedded_jpeg_with_exif_is_reencoded_without_metadata():
    import base64
    b64, raw = _png_b64(fmt="JPEG")
    assert b"secret-description-marker" in raw
    out = clean(f'<image width="1" height="1" href="data:image/jpeg;base64,{b64}"/>')
    uri = out.split('href="')[1].split('"')[0]
    assert uri.startswith("data:image/jpeg;base64,")
    new = base64.b64decode(uri.split(",", 1)[1])
    assert b"secret-description-marker" not in new and new != raw and new[:3] == b"\xff\xd8\xff"


def test_embedded_png_text_chunk_is_gone():
    import base64
    import io

    from PIL import Image, PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret-description-marker")
    buf = io.BytesIO()
    Image.new("RGB", (3, 3)).save(buf, "PNG", pnginfo=info)
    assert b"secret-description-marker" in buf.getvalue()
    out = clean(f'<image width="1" height="1" href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/>')
    new = base64.b64decode(out.split("base64,")[1].split('"')[0])
    assert b"secret-description-marker" not in new and Image.open(io.BytesIO(new)).size == (3, 3)


@pytest.mark.parametrize("payload", [
    "iVBORw0KGgo=",  # a truncated PNG signature
    "bm90LWFuLWltYWdl",  # decodes fine, is not an image
    "PGh0bWw+PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",  # html polyglot body
    "AAAAA",  # bad base64 padding
])
def test_garbage_embedded_image_refuses_the_svg(payload):
    err = refuses(wrap(f'<image width="1" height="1" href="data:image/png;base64,{payload}"/>'), "corrupt")
    assert "embedded" in err.message


def test_png_header_with_garbage_body_refuses():
    import base64
    b64, raw = _png_b64(exif=False)
    bad = base64.b64encode(raw[:40]).decode()
    refuses(wrap(f'<image width="1" height="1" href="data:image/png;base64,{bad}"/>'), "corrupt")


def test_too_many_embedded_images_refused(monkeypatch):
    b64, _ = _png_b64(exif=False)
    n = config.KAISEN_UPLOAD_SVG_MAX_IMAGES + 1
    inner = "".join(f'<image width="1" height="1" href="data:image/png;base64,{b64}"/>' for _ in range(n))
    err = refuses(wrap(inner), "too_big")
    assert str(config.KAISEN_UPLOAD_SVG_MAX_IMAGES) in err.message


def test_embedded_image_over_pixel_cap_refused(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_MAX_PIXELS", 10)
    b64, _ = _png_b64(size=(8, 8), exif=False)
    err = refuses(wrap(f'<image width="1" height="1" href="data:image/png;base64,{b64}"/>'), "too_big")
    assert "embedded" in err.message


def test_total_embedded_pixels_capped(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_IMAGE_PIXELS", 30)
    b64, _ = _png_b64(size=(4, 4), exif=False)
    inner = "".join(f'<image width="1" height="1" href="data:image/png;base64,{b64}"/>' for _ in range(2))
    refuses(wrap(inner), "too_big")  # 2 x 16 px > 30 in total
    assert clean(inner.split("/>")[0] + "/>").count("<image") == 1  # one picture alone fits


def test_svg_still_capped_after_reencoding(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_BYTES", 400)
    b64, _ = _png_b64(exif=False)
    refuses(wrap(f'<image width="1" height="1" href="data:image/png;base64,{b64}"/><rect width="1" height="1"/>' * 3), "too_big")


def test_unlisted_attributes_are_dropped():
    out = clean('<rect width="1" height="1" data-x="1" xml:lang="en" xlink:show="new" xlink:actuate="onLoad" formaction="x" srcdoc="y"/>')
    for bad in ("data-x", "show", "actuate", "formaction", "srcdoc"):
        assert bad not in out


def test_bad_id_and_class_values_are_dropped():
    out = clean('<rect id="a b" class="x&quot; onload=&quot;y" width="1" height="1"/>')
    assert "onload" not in out and 'id="a b"' not in out


def test_comments_processing_instructions_and_cdata_do_not_pass_through():
    out = core.process(b'<?xml version="1.0"?><?xml-stylesheet href="https://evil.example/s.css"?><!-- <script>alert(1)</script> -->'
                       b'<svg xmlns="http://www.w3.org/2000/svg"><title><![CDATA[a <b> c]]></title><rect width="1" height="1"/></svg>',
                       "svg").data.decode()
    assert "evil" not in out and "script" not in out and "<!--" not in out and "<?" not in out
    assert "a &lt;b&gt; c" in out


def test_text_is_escaped_on_the_way_out():
    out = clean('<text>&lt;script&gt;alert(1)&lt;/script&gt;</text>')
    assert "<script" not in out and "&lt;script&gt;" in out


# --- XML-level attacks ------------------------------------------------------------------------------------------

def test_billion_laughs_is_refused():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg"><text>&d;</text></svg>')
    refuses(bomb, "corrupt")


def test_external_dtd_is_refused():
    refuses(b'<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "https://evil.example/svg11.dtd">'
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>', "corrupt")


def test_external_entity_file_read_is_refused():
    refuses(b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>', "corrupt")


def test_internal_doctype_without_entities_is_still_refused():
    refuses(b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>', "corrupt")


def test_not_xml_and_not_svg_are_refused():
    refuses(b"<svg><rect></svg>", "corrupt")
    refuses(b"\x00\x01 not xml", "corrupt")
    refuses(b'<html xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></html>', "unsupported")
    refuses(b'<svg><rect width="1" height="1"/></svg>', "unsupported")  # no SVG namespace


def test_size_depth_and_element_caps(monkeypatch):
    refuses(b"<svg xmlns='http://www.w3.org/2000/svg'>" + b"<g/>" * 200000 + b"</svg>", "too_big")  # > 512 KiB input
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_ELEMENTS", 50)
    refuses(wrap("<g/>" * 60), "too_big")
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_ELEMENTS", 5000)
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_MAX_DEPTH", 5)
    refuses(wrap("<g>" * 8 + "</g>" * 8), "too_big")


def test_output_is_capped_at_512_kib(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_UPLOAD_SVG_BYTES", 300)
    # input just under the cap with text that still fits; then one that is only over after nothing is dropped
    refuses(wrap("<text>" + "a" * 400 + "</text>"), "too_big")


def test_xlink_namespace_trick_with_other_prefix_is_still_checked():
    out = clean('<use xmlns:z="http://www.w3.org/1999/xlink" z:href="javascript:alert(1)"/>')
    assert "javascript" not in out and "<use" not in out


def test_sanitizer_output_is_idempotent():
    first = clean('<g id="a"><rect width="3" height="3" fill="#123"/></g><use href="#a"/>')
    assert core.process(first.encode(), "svg").data.decode() == first


def test_module_constants_are_the_expected_allowlist():
    assert "script" not in svg.ELEMENTS and "foreignObject" not in svg.ELEMENTS and "a" not in svg.ELEMENTS
    assert not any(a.lower().startswith("on") for a in svg.ATTRS)
