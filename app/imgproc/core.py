"""Entry point shared by the HTTP server and the in-process fake: bytes in, ProcessedImage out (or ImageProcessError)."""
from app.providers.base import ImageProcessError, ProcessedImage

from . import raster, svg


def process(data: bytes, hint: str) -> ProcessedImage:
    if hint == "svg":
        return svg.sanitize(data)
    if hint == "raster":
        return raster.process(data)
    raise ImageProcessError("unsupported", "That file type isn't supported.")
