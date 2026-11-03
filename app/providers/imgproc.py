"""Real ImageProcessor: HTTP to the secret-less kaisen-imgproc pod (app/imgproc/server.py). Bytes in, bytes out, hard
caps both ways, a timeout, and no knowledge of what the pod does with the bytes."""
import json

import httpx

from app import config
from app.providers.base import ImageProcessError, ImageProcessor, ProcessedImage

_UNAVAILABLE = ImageProcessError("unavailable", "Image processing isn't available right now. Try again in a minute.")


class HttpImageProcessor(ImageProcessor):
    def __init__(self, base_url: str | None = None, timeout: float | None = None, transport=None):
        self.base_url = (base_url or config.KAISEN_IMGPROC_URL).rstrip("/")
        self.timeout = timeout or config.KAISEN_UPLOAD_IMG_TIMEOUT
        self._transport = transport

    def process(self, data: bytes, hint: str) -> ProcessedImage:
        cap = config.KAISEN_UPLOAD_MAX_BYTES_OUT
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport, follow_redirects=False) as c:
                with c.stream("POST", f"{self.base_url}/process", params={"hint": hint}, content=data,
                              headers={"Content-Type": "application/octet-stream"}) as r:
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > cap:
                            raise ImageProcessError("too_big", "That image is still too large after processing.")
                    status, headers = r.status_code, r.headers
        except ImageProcessError:
            raise
        except httpx.TimeoutException:
            raise ImageProcessError("timeout", "That file took too long to process.") from None
        except httpx.HTTPError:
            raise _UNAVAILABLE from None
        if status == 200:
            try:
                w, h = headers.get("X-Width", ""), headers.get("X-Height", "")
                notes = json.loads(headers.get("X-Notes", "[]"))
                if not isinstance(notes, list):
                    raise ValueError
                return ProcessedImage(mime=headers.get("X-Mime", ""), data=bytes(body), width=int(w) if w else None,
                                      height=int(h) if h else None, notes=[str(n)[:200] for n in notes[:10]])
            except ValueError:
                raise _UNAVAILABLE from None
        try:
            info = json.loads(bytes(body[:2000]))
            code, message = str(info["code"]), str(info["message"])[:300]
        except (ValueError, KeyError, TypeError):
            raise _UNAVAILABLE from None
        if code not in ("unsupported", "corrupt", "too_big", "animated", "timeout", "busy"):
            code = "corrupt"
        raise ImageProcessError(code, message)
