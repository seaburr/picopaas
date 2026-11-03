"""Image bytes built at runtime for the upload tests (nothing binary is committed, nothing looks like a secret)."""
import io

from PIL import Image


def png(w=64, h=48, color=(200, 30, 30), mode="RGB", text=None):
    im = Image.new(mode, (w, h), color if mode in ("RGB", "RGBA") else 128)
    buf = io.BytesIO()
    kw = {}
    if text:
        from PIL import PngImagePlugin
        info = PngImagePlugin.PngInfo()
        for k, v in text.items():
            info.add_text(k, v)
        kw["pnginfo"] = info
    im.save(buf, "PNG", **kw)
    return buf.getvalue()


def jpeg(w=64, h=48, exif=True, icc=True, orientation=None):
    im = Image.new("RGB", (w, h), (10, 120, 200))
    ex = Image.Exif()
    if exif:
        ex[0x010F] = "CameraMakerXYZ"  # Make
        ex[0x0110] = "SerialModel9"  # Model
        gps = ex.get_ifd(0x8825)
        gps[1], gps[2], gps[3], gps[4] = "N", (40.0, 26.0, 46.0), "W", (79.0, 58.0, 56.0)
    if orientation:
        ex[0x0112] = orientation
    buf = io.BytesIO()
    kw = {"exif": ex.tobytes()} if (exif or orientation) else {}
    if icc:
        from PIL import ImageCms
        kw["icc_profile"] = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def webp(w=64, h=48, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), (5, 5, 5, 255) if mode == "RGBA" else (5, 5, 5)).save(buf, "WEBP")
    return buf.getvalue()


def animated_webp():
    frames = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=100)
    return buf.getvalue()


def apng():
    frames = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=100)
    return buf.getvalue()


def opened(data):
    return Image.open(io.BytesIO(data))
