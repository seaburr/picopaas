"""Raster uploads: full decode, then a fresh encode from pixels (no metadata copied). Hardening per 16.8 R2."""
import io
import warnings

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError
from PIL import features

from app import config
from app.providers.base import ImageProcessError, ProcessedImage

FORMATS = ["PNG", "JPEG", "WEBP"]
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = config.KAISEN_UPLOAD_MAX_PIXELS


def libwebp_ok() -> bool:
    """True when Pillow's bundled libwebp is at or above the fixed version, so its encoder may be used."""
    try:
        have = tuple(int(x) for x in (features.version("webp") or "0").split(".")[:3])
    except ValueError:
        return False
    return have >= config.KAISEN_UPLOAD_MIN_LIBWEBP


def _gray16(img):
    return img.point(lambda v: v * (1 / 256)).convert("L")


def _normalize(img):
    """Explicit mode conversion to L, RGB or RGBA (never a mode an encoder might handle oddly)."""
    mode = img.mode
    if mode in ("RGB", "RGBA", "L"):
        return img
    if mode in ("I", "F") or mode.startswith("I;16"):
        return _gray16(img)
    if mode in ("LA", "La", "PA", "RGBa") or (mode == "P" and "transparency" in img.info):
        return img.convert("RGBA")
    if mode == "1":
        return img.convert("L")
    return img.convert("RGB")  # CMYK, YCbCr, LAB, HSV, P


def _flatten(img):
    """Drop an alpha channel that is fully opaque (smaller file, JPEG-compatible)."""
    if img.mode == "RGBA":
        lo, _hi = img.getchannel("A").getextrema()
        if lo == 255:
            return img.convert("RGB")
    return img


def process(data: bytes) -> ProcessedImage:
    if len(data) > config.KAISEN_UPLOAD_MAX_BYTES_IN:
        raise ImageProcessError("too_big", "That file is too large.")
    notes: list[str] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data), formats=FORMATS)
            fmt = img.format
            w, h = img.size
            if w * h > config.KAISEN_UPLOAD_MAX_PIXELS or max(w, h) > config.KAISEN_UPLOAD_MAX_EDGE_IN:
                raise ImageProcessError("too_big", "That image has too many pixels. Use one under "
                                        f"{config.KAISEN_UPLOAD_MAX_PIXELS // 1_000_000} megapixels.")
            if getattr(img, "n_frames", 1) > 1 or getattr(img, "is_animated", False):
                raise ImageProcessError("animated", "Animated images aren't supported. Upload a single still image.")
            edge = config.KAISEN_UPLOAD_MAX_EDGE
            if fmt == "JPEG" and max(w, h) > edge * 2:
                img.draft("RGB", (max(1, w * edge // max(w, h)), max(1, h * edge // max(w, h))))  # decode at reduced size
            img.load()
            img = ImageOps.exif_transpose(img)
            img = _normalize(img)
            img = _flatten(img)
            nw, nh = img.size
            if max(nw, nh) > edge:
                scale = edge / max(nw, nh)
                img = img.resize((max(1, round(nw * scale)), max(1, round(nh * scale))), Image.LANCZOS)
                notes.append(f"Resized {w}x{h} to {img.size[0]}x{img.size[1]}")
            out_fmt = fmt
            if fmt == "WEBP" and not libwebp_ok():
                out_fmt = "PNG" if img.mode == "RGBA" else "JPEG"
                notes.append(f"Saved as {out_fmt.lower()}")
            if out_fmt == "JPEG" and img.mode != "RGB":
                if img.mode == "RGBA":
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.getchannel("A"))
                    img = bg
                else:
                    img = img.convert("RGB")
            img.info = {}  # nothing from the source (EXIF, ICC, XMP, text chunks) can reach the encoder
            buf = io.BytesIO()
            if out_fmt == "PNG":
                img.save(buf, "PNG", optimize=True)
            elif out_fmt == "JPEG":
                img.save(buf, "JPEG", quality=config.KAISEN_UPLOAD_JPEG_QUALITY, optimize=True)
            else:
                img.save(buf, "WEBP", quality=config.KAISEN_UPLOAD_WEBP_QUALITY, method=4)
    except ImageProcessError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
        raise ImageProcessError("too_big", "That image has too many pixels.") from None
    except UnidentifiedImageError:
        if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
            raise ImageProcessError("corrupt", "That image couldn't be read. It may be damaged; try exporting it again.") from None
        raise ImageProcessError("unsupported", "That file type isn't supported. Use a PNG, JPEG or WebP image.") from None
    except Exception:  # noqa: BLE001 - truncated/corrupt streams raise OSError, SyntaxError, ValueError, struct.error ...
        raise ImageProcessError("corrupt", "That image couldn't be read. It may be damaged; try exporting it again.") from None
    out = buf.getvalue()
    if len(out) > config.KAISEN_UPLOAD_MAX_BYTES_OUT:
        raise ImageProcessError("too_big", "That image is still too large after processing.")
    notes.insert(0, "Metadata removed")
    return ProcessedImage(mime=_MIME[out_fmt], data=out, width=img.size[0], height=img.size[1], notes=notes)
