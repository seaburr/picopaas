"""Strict SVG sanitizer (Chas 2026-11-03: SVG allowed only after sanitizing).

Parse with defusedxml (no DTD, no entities, no external references), copy only allow-listed elements and attributes into
a new tree, drop everything else, and write the XML ourselves. The result is meant to be shown as an image: it contains
no script, no foreignObject, no event handlers, no animation, no external reference of any kind (links are local
#fragments or data:image/(png|jpeg|webp) URIs), and no CSS that can fetch or execute anything."""
import base64
import binascii
import io
import re
from xml.sax.saxutils import escape, quoteattr

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from PIL import Image

from app import config
from app.imgproc import raster
from app.providers.base import ImageProcessError, ProcessedImage

from .fmt import kb, megapixels

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ELEMENTS = frozenset({
    "svg", "g", "defs", "symbol", "use", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "title", "desc", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern",
    "image", "style", "marker",
    "filter", "feGaussianBlur", "feOffset", "feBlend", "feColorMatrix", "feFlood", "feMerge", "feMergeNode",
    "feComposite", "feDropShadow",
})
TEXT_ELEMENTS = frozenset({"text", "tspan", "title", "desc", "style"})
# Elements dropped WITH their children (everything not listed above is dropped too; these are called out because they are
# the classic vectors): script, foreignObject, a, animate*, set, iframe, object, embed, feImage, textPath, switch.

ATTRS = frozenset({
    "id", "class", "style", "transform", "d", "x", "y", "width", "height", "cx", "cy", "r", "rx", "ry", "x1", "y1", "x2", "y2",
    "points", "viewBox", "preserveAspectRatio", "version", "fill", "stroke", "opacity", "fill-opacity", "fill-rule",
    "clip-rule", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray",
    "stroke-dashoffset", "stroke-opacity", "clip-path", "mask", "filter", "color", "display", "visibility", "overflow",
    "vector-effect", "font-family", "font-size", "font-weight", "font-style", "text-anchor", "dominant-baseline",
    "letter-spacing", "word-spacing", "text-decoration", "dx", "dy", "rotate", "textLength", "lengthAdjust", "offset",
    "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod", "fx", "fy", "fr", "patternUnits",
    "patternTransform", "patternContentUnits", "clipPathUnits", "maskUnits", "maskContentUnits", "markerWidth",
    "markerHeight", "markerUnits", "refX", "refY", "orient", "marker-start", "marker-mid", "marker-end", "in", "in2",
    "result", "stdDeviation", "mode", "values", "type", "flood-color", "flood-opacity", "operator", "k1", "k2", "k3", "k4",
    "floodColor", "role", "aria-label", "aria-hidden", "lang", "shape-rendering", "image-rendering", "paint-order",
    "filterUnits", "primitiveUnits", "dx", "dy",
})
_FRAGMENT = re.compile(r"#[A-Za-z_][\w.:-]{0,100}")
_DATA_IMG = re.compile(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=\s]+")
_ID = re.compile(r"[A-Za-z_][\w.:-]{0,100}")
_CLASS = re.compile(r"[\w\s.-]{0,200}")
_URL = re.compile(r"url\(", re.I)
_LOCAL_URL = re.compile(r"url\(\s*(['\"]?)#[\w.:-]+\1\s*\)", re.I)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FUNC = re.compile(r"([-\w]+)\s*\(")
# CSS functions a value may use. Anything else is dropped: image-set()/src()/image()/cross-fade() take a plain string URL
# (no url() token) and would fetch an external resource (tracking beacon) when the SVG is opened or inlined (K76).
_SAFE_FUNCS = frozenset({
    "url", "rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp", "var", "translate", "translatex", "translatey",
    "translatez", "translate3d", "scale", "scalex", "scaley", "scale3d", "rotate", "rotatex", "rotatey", "rotatez",
    "skew", "skewx", "skewy", "matrix", "matrix3d", "perspective", "blur", "brightness", "contrast", "drop-shadow",
    "grayscale", "hue-rotate", "invert", "opacity", "saturate", "sepia", "linear-gradient", "radial-gradient",
    "repeating-linear-gradient", "repeating-radial-gradient", "conic-gradient", "format", "local",
    "not", "is", "where", "has", "nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type", "lang",  # selectors
})
_BAD_CSS = ("expression", "javascript:", "vbscript:", "behavior", "binding", "data:", "@")


def css_ok(value: str) -> bool:
    """True for a style value/sheet that cannot fetch or execute anything: no @-rules (@import, @font-face), no backslash
    escapes, no expression()/javascript:/data:, and every url() a local #fragment."""
    v = _CSS_COMMENT.sub("", value)
    if "\\" in v or "<" in v or _CTRL.search(v):
        return False
    squashed = re.sub(r"\s+", "", v).lower()
    if any(bad in squashed for bad in _BAD_CSS):
        return False
    if any(f.lower() not in _SAFE_FUNCS for f in _FUNC.findall(v)):
        return False
    return len(_URL.findall(v)) == len(_LOCAL_URL.findall(v))


def _local(tag: str) -> str | None:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return name if ns == SVG_NS else None
    return None


def _attr(name: str):
    """(output name, local name) for an attribute we may keep, else None."""
    if name.startswith("{"):
        ns, _, local = name[1:].partition("}")
        if ns == XLINK_NS and local == "href":
            return "xlink:href", "href"
        if ns == XML_NS and local == "space":
            return "xml:space", "space"
        return None
    if name == "href":
        return "href", "href"
    return (name, name) if name in ATTRS else None


class _Node:
    __slots__ = ("tag", "attrs", "text", "children")

    def __init__(self, tag):
        self.tag, self.attrs, self.text, self.children = tag, {}, "", []


def _clean_text(t: str | None) -> str:
    return _CTRL.sub("", t or "")


def _convert(el, depth: int, budget: list[int]):
    name = _local(el.tag)
    if name is None or name not in ELEMENTS:
        return None
    budget[0] -= 1
    if budget[0] < 0:
        raise ImageProcessError("too_big", f"That SVG has more than {config.KAISEN_UPLOAD_SVG_MAX_ELEMENTS} elements, the most allowed. Simplify it or "
                                           "export it with fewer shapes.")
    if depth > config.KAISEN_UPLOAD_SVG_MAX_DEPTH:
        raise ImageProcessError("too_big", f"That SVG is nested more than {config.KAISEN_UPLOAD_SVG_MAX_DEPTH} levels deep, the most allowed. "
                                           "Flatten its groups and export it again.")
    node = _Node(name)
    for raw, val in el.attrib.items():
        got = _attr(raw)
        if got is None:
            continue
        out, local = got
        if local == "href":
            if _FRAGMENT.fullmatch(val.strip()) and name in ("use", "linearGradient", "radialGradient", "pattern"):
                node.attrs[out] = val.strip()
            elif name == "image" and _DATA_IMG.fullmatch(val.strip()):
                node.attrs[out] = re.sub(r"\s+", "", val)
            continue
        if out == "id" and not _ID.fullmatch(val):
            continue
        if out == "class" and not _CLASS.fullmatch(val):
            continue
        if _CTRL.search(val) or not css_ok(val):
            continue
        node.attrs[out] = val
    if name == "use" and "href" not in node.attrs and "xlink:href" not in node.attrs:
        return None
    if name == "image" and "href" not in node.attrs and "xlink:href" not in node.attrs:
        return None
    if name in TEXT_ELEMENTS:
        node.text = _clean_text(el.text)
        if name == "style" and not css_ok(node.text):
            return None
    for child in el:
        c = _convert(child, depth + 1, budget)
        if c is not None:
            node.children.append(c)
        if name in ("text", "tspan") and child.tail:
            tail = _Node("#text")
            tail.text = _clean_text(child.tail)
            node.children.append(tail)
    return node


def _walk(node):
    yield node
    for c in node.children:
        yield from _walk(c)


def _drop_unsafe_use(root: _Node) -> None:
    """A <use> must point at an existing element that holds no <use> itself: no reference chains, no exponential expansion."""
    ids = {n.attrs["id"]: n for n in _walk(root) if n.tag != "#text" and "id" in n.attrs}

    def bad(n: _Node) -> bool:
        if n.tag != "use":
            return False
        ref = n.attrs.get("href") or n.attrs.get("xlink:href") or ""
        target = ids.get(ref[1:])
        return target is None or any(d.tag == "use" for d in _walk(target)) or any(d is n for d in _walk(target))

    def prune(n: _Node) -> None:
        n.children = [c for c in n.children if not bad(c)]
        for c in n.children:
            prune(c)

    prune(root)


def _reencode_images(root: _Node) -> None:
    """K79: every embedded bitmap goes through the same Pillow pipeline as an upload (metadata stripped, size caps, no
    animation) and the data URI is replaced by the fresh encode. Any failure refuses the whole SVG."""
    images = [n for n in _walk(root) if n.tag == "image"]
    if len(images) > config.KAISEN_UPLOAD_SVG_MAX_IMAGES:
        raise ImageProcessError("too_big", f"That SVG embeds {len(images)} pictures. The most allowed is "
                                           f"{config.KAISEN_UPLOAD_SVG_MAX_IMAGES}; use fewer, or upload them as separate images.")
    total = 0
    for n in images:
        for key in [k for k in ("href", "xlink:href") if k in n.attrs]:  # both may be present; re-encode both
            uri = n.attrs[key]
            try:
                raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
            except (binascii.Error, ValueError, IndexError):
                raise ImageProcessError("corrupt", "A picture embedded in that SVG couldn't be read. "
                                                   "Export the SVG again.") from None
            try:
                with Image.open(io.BytesIO(raw), formats=raster.FORMATS) as probe:
                    total += probe.size[0] * probe.size[1]
            except Exception:  # noqa: BLE001 - any decoder error means the embedded picture is unusable
                raise ImageProcessError("corrupt", "A picture embedded in that SVG couldn't be read. "
                                                   "Export the SVG again.") from None
            if total > config.KAISEN_UPLOAD_SVG_MAX_IMAGE_PIXELS:
                raise ImageProcessError("too_big", (f"The pictures embedded in that SVG add up to {megapixels(total)}. The most allowed in total is "
                                                f"{megapixels(config.KAISEN_UPLOAD_SVG_MAX_IMAGE_PIXELS)}; use smaller pictures."))
            try:
                res = raster.process(raw)
            except ImageProcessError as exc:
                raise ImageProcessError(exc.code, f"A picture embedded in that SVG was refused: {exc.message}") from None
            n.attrs[key] = f"data:{res.mime};base64," + base64.b64encode(res.data).decode()


def _write(node: _Node, out: list[str]) -> None:
    if node.tag == "#text":
        out.append(escape(node.text))
        return
    attrs = "".join(f" {k}={quoteattr(v)}" for k, v in node.attrs.items())
    if node.tag == "svg":
        attrs = f' xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}"' + attrs
    if not node.children and not node.text:
        out.append(f"<{node.tag}{attrs}/>")
        return
    out.append(f"<{node.tag}{attrs}>{escape(node.text)}")
    for c in node.children:
        _write(c, out)
    out.append(f"</{node.tag}>")


def _px(v: str | None) -> int | None:
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*(px)?\s*", v or "")
    return max(1, round(float(m.group(1)))) if m else None


def _size(root: _Node) -> tuple[int | None, int | None]:
    w, h = _px(root.attrs.get("width")), _px(root.attrs.get("height"))
    if w is None or h is None:
        parts = re.split(r"[\s,]+", (root.attrs.get("viewBox") or "").strip())
        if len(parts) == 4:
            try:
                vw, vh = float(parts[2]), float(parts[3])
                w, h = w or max(1, round(vw)), h or max(1, round(vh))
            except ValueError:
                pass
    return w, h


def sanitize(data: bytes) -> ProcessedImage:
    if len(data) > config.KAISEN_UPLOAD_SVG_BYTES:
        raise ImageProcessError("too_big", f"That SVG is {kb(len(data))}. SVG files can be at most "
                                           f"{kb(config.KAISEN_UPLOAD_SVG_BYTES)}; simplify it and try again.")
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise ImageProcessError("corrupt", "That SVG uses a DOCTYPE, entities or external references, which aren't "
                                           "allowed. Export it as a plain SVG.") from None
    except (ParseError, ValueError, UnicodeError):
        raise ImageProcessError("corrupt", "That SVG couldn't be read. It may be damaged; try exporting it again.") from None
    if _local(root.tag) != "svg":
        raise ImageProcessError("unsupported", "That file isn't an SVG image.")
    node = _convert(root, 0, [config.KAISEN_UPLOAD_SVG_MAX_ELEMENTS])
    if node is None:  # pragma: no cover - root is svg, which is allow-listed
        raise ImageProcessError("unsupported", "That file isn't an SVG image.")
    _drop_unsafe_use(node)
    _reencode_images(node)
    parts: list[str] = []
    _write(node, parts)
    out = "".join(parts).encode("utf-8")
    if len(out) > config.KAISEN_UPLOAD_SVG_BYTES:
        raise ImageProcessError("too_big", f"That SVG is {kb(len(out))} after cleaning. The most allowed is "
                                           f"{kb(config.KAISEN_UPLOAD_SVG_BYTES)}; simplify it and try again.")
    w, h = _size(node)
    return ProcessedImage(mime="image/svg+xml", data=out, width=w, height=h,
                          notes=["Scripts, external links and animation removed from the SVG"])
