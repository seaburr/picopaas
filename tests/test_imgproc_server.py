"""The image pod over real HTTP: server (app/imgproc/server.py) + the real client (app/providers/imgproc.py), loopback only."""
import threading

import httpx
import pytest

from app import config
from app.imgproc import server
from app.providers.base import ImageProcessError
from app.providers.imgproc import HttpImageProcessor
from tests import imgfix


@pytest.fixture()
def pod():
    srv = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url
    srv.shutdown()
    srv.server_close()


def test_healthz(pod):
    assert httpx.get(pod + "/healthz").text == "ok"
    assert httpx.get(pod + "/nope").status_code == 404


def test_client_roundtrip_raster_and_svg(pod):
    c = HttpImageProcessor(pod)
    res = c.process(imgfix.jpeg(), "raster")
    assert res.mime == "image/jpeg" and (res.width, res.height) == (64, 48) and "Metadata removed" in res.notes
    assert b"CameraMakerXYZ" not in res.data
    sv = c.process(b'<svg xmlns="http://www.w3.org/2000/svg" width="9" height="8"><script>x</script><rect width="1" height="1"/></svg>', "svg")
    assert sv.mime == "image/svg+xml" and b"script" not in sv.data and (sv.width, sv.height) == (9, 8)


@pytest.mark.parametrize("data,hint,code", [
    (b"not an image", "raster", "unsupported"),
    (imgfix.animated_webp(), "raster", "animated"),
    (imgfix.png()[:40], "raster", "corrupt"),
    (b"<!DOCTYPE svg><svg xmlns='http://www.w3.org/2000/svg'/>", "svg", "corrupt"),
    (b"x", "other", "unsupported"),
])
def test_errors_keep_their_code_and_plain_message(pod, data, hint, code):
    with pytest.raises(ImageProcessError) as ei:
        HttpImageProcessor(pod).process(data, hint)
    assert ei.value.code == code and ei.value.message


def test_server_refuses_chunked_and_oversized_bodies(pod):
    r = httpx.post(pod + "/process?hint=raster", content=iter([b"abc", b"def"]))
    assert r.status_code == 422 and r.json()["code"] == "too_big"
    big = httpx.post(pod + "/process?hint=raster", content=b"0" * (config.KAISEN_UPLOAD_MAX_BYTES_IN + 10))
    assert big.status_code == 422 and big.json()["code"] == "too_big"
    assert httpx.post(pod + "/elsewhere", content=b"x").status_code == 404


def test_server_never_leaks_internals_on_a_crash(pod, monkeypatch):
    from app.imgproc import core

    def boom(data, hint):
        raise RuntimeError("secret internal path /srv/x")
    monkeypatch.setattr(core, "process", boom)
    r = httpx.post(pod + "/process?hint=raster", content=b"x")
    assert r.status_code == 422 and "secret internal" not in r.text


def test_client_maps_outages_and_garbage_replies():
    dead = HttpImageProcessor("http://127.0.0.1:1", timeout=1)
    with pytest.raises(ImageProcessError) as ei:
        dead.process(b"x", "raster")
    assert ei.value.code == "unavailable"

    def handler(request):
        return httpx.Response(200, content=b"<html>", headers={"X-Notes": "{not json"})
    odd = HttpImageProcessor("http://pod", transport=httpx.MockTransport(handler))
    with pytest.raises(ImageProcessError) as ei:
        odd.process(b"x", "raster")
    assert ei.value.code == "unavailable"

    def slow(request):
        raise httpx.ReadTimeout("slow")
    with pytest.raises(ImageProcessError) as ei:
        HttpImageProcessor("http://pod", transport=httpx.MockTransport(slow)).process(b"x", "raster")
    assert ei.value.code == "timeout"

    def huge(request):
        return httpx.Response(200, content=b"0" * (config.KAISEN_UPLOAD_MAX_BYTES_OUT + 1), headers={"X-Mime": "image/png"})
    with pytest.raises(ImageProcessError) as ei:
        HttpImageProcessor("http://pod", transport=httpx.MockTransport(huge)).process(b"x", "raster")
    assert ei.value.code == "too_big"

    def odd_error(request):
        return httpx.Response(500, content=b"oops")
    with pytest.raises(ImageProcessError) as ei:
        HttpImageProcessor("http://pod", transport=httpx.MockTransport(odd_error)).process(b"x", "raster")
    assert ei.value.code == "unavailable"

    def weird_code(request):
        return httpx.Response(422, json={"code": "made-up", "message": "m"})
    with pytest.raises(ImageProcessError) as ei:
        HttpImageProcessor("http://pod", transport=httpx.MockTransport(weird_code)).process(b"x", "raster")
    assert ei.value.code == "corrupt"


def test_server_maps_timeouts_and_memory_errors(pod, monkeypatch):
    from app.imgproc import core

    def slow(data, hint):
        raise server._Timeout()
    monkeypatch.setattr(core, "process", slow)
    r = httpx.post(pod + "/process?hint=raster", content=b"x")
    assert r.status_code == 504 and r.json()["code"] == "timeout"

    def hungry(data, hint):
        raise MemoryError()
    monkeypatch.setattr(core, "process", hungry)
    r = httpx.post(pod + "/process?hint=raster", content=b"x")
    assert r.status_code == 422 and r.json()["code"] == "too_big"
    with pytest.raises(server._Timeout):
        server._on_alarm(14, None)


def test_server_rejects_a_missing_content_length(pod):
    import socket
    host, port = pod.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall(b"POST /process?hint=raster HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        reply = b""
        while chunk := s.recv(4096):  # the server closes after answering (Connection: close)
            reply += chunk
    assert b"422" in reply.split(b"\r\n")[0] and b"too_big" in reply


def _raise_value_error(*_a):
    raise ValueError("current limit exceeds maximum limit")


def test_memory_limit_is_best_effort_only_on_macos(monkeypatch, caplog):
    import resource
    import sys

    from app.imgproc import server
    monkeypatch.setattr(resource, "setrlimit", _raise_value_error)
    monkeypatch.setattr(sys, "platform", "darwin")
    server.apply_memory_limit()  # warns, does not raise
    assert "address-space limit" in caplog.text
    monkeypatch.setattr(sys, "platform", "linux")
    import pytest
    with pytest.raises(ValueError):
        server.apply_memory_limit()
