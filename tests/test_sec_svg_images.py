"""K85/K86: SVG embedded bitmaps are re-encoded under both href spellings; an image without a usable href is dropped, not a 500."""
import base64
import io

from PIL import Image

from app.imgproc import svg

NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


def _png(marker: bytes) -> str:
    buf = io.BytesIO()
    im = Image.new("RGB", (4, 4), (1, 2, 3))
    from PIL import PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", marker.decode())
    im.save(buf, "PNG", pnginfo=info)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_both_href_spellings_are_reencoded():
    uri = _png(b"hidden-marker")
    src = f'<svg {NS}><image width="4" height="4" href="{uri}" xlink:href="{uri}"/></svg>'.encode()
    out = svg.sanitize(src).data.decode()
    assert out.count("data:image/png;base64,") == 2
    for part in out.split('"'):
        if part.startswith("data:image/"):
            assert b"hidden-marker" not in base64.b64decode(part.split(",", 1)[1])


def test_image_without_usable_href_does_not_crash():
    src = f'<svg {NS}><image width="4" height="4" href="http://example.invalid/x.png"/></svg>'.encode()
    assert b"example.invalid" not in svg.sanitize(src).data
