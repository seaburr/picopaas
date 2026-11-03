"""P1b: the preview runtime's static server (images/kaisen-runtime/boot.py): nosniff everywhere, strict CSP and fixed
Content-Type under uploads/, no listing there, and the .kaisen-uploads.json manifest check before serving."""
import hashlib
import http.client
import importlib.util
import json
import threading
from pathlib import Path

import pytest

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1"/></svg>'
PNG = b"\x89PNG\r\n\x1a\n" + b"not-really-a-png"


@pytest.fixture(scope="module")
def boot():
    spec = importlib.util.spec_from_file_location("kaisen_boot_p1b", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _site(tmp_path):
    (tmp_path / "uploads").mkdir()
    (tmp_path / "index.html").write_text("<p>hi</p>")
    (tmp_path / "uploads" / "a.svg").write_bytes(SVG)
    (tmp_path / "uploads" / "b.png").write_bytes(PNG)
    (tmp_path / "uploads" / "c.txt").write_text("plain")
    (tmp_path / "other.svg").write_bytes(SVG)
    return tmp_path


@pytest.fixture
def server(boot, tmp_path):
    import functools
    import http.server
    root = _site(tmp_path)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(boot.StaticHandler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _get(port, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def test_normal_pages_get_nosniff_only(server):
    status, h, body = _get(server, "/index.html")
    assert status == 200 and body == b"<p>hi</p>" and h["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in h and h["content-type"].startswith("text/html")


def test_svg_outside_uploads_is_not_given_a_csp(server):
    _, h, _ = _get(server, "/other.svg")
    assert h["x-content-type-options"] == "nosniff" and "content-security-policy" not in h


def test_uploaded_svg_has_fixed_type_and_sandboxing_csp(server, boot):
    status, h, body = _get(server, "/uploads/a.svg")
    assert status == 200 and body == SVG and h["content-type"] == "image/svg+xml"
    assert h["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
    assert h["x-content-type-options"] == "nosniff"


def test_uploaded_raster_has_strict_csp(server):
    status, h, _ = _get(server, "/uploads/b.png")
    assert status == 200 and h["content-type"] == "image/png"
    assert h["content-security-policy"] == "default-src 'none'; sandbox" and h["x-content-type-options"] == "nosniff"


def test_other_uploads_files_still_get_csp_and_nosniff(server):
    _, h, _ = _get(server, "/uploads/c.txt")
    assert h["content-security-policy"] == "default-src 'none'; sandbox" and h["x-content-type-options"] == "nosniff"


def test_head_requests_and_query_strings_get_the_same_headers(server):
    _, h, _ = _get(server, "/uploads/a.svg?x=1", "HEAD")
    assert "sandbox" in h["content-security-policy"] and h["content-type"] == "image/svg+xml"


def test_dot_dot_and_encoded_paths_into_uploads_are_covered(server):
    for path in ("/x/../uploads/a.svg", "/%75ploads/a.svg", "//uploads/a.svg"):
        status, h, _ = _get(server, path)
        assert status == 200 and "sandbox" in h["content-security-policy"], path


def test_uploads_directory_listing_is_refused_and_errors_carry_nosniff(server):
    status, h, _ = _get(server, "/uploads/")
    assert status == 404 and h["x-content-type-options"] == "nosniff"
    status, h, _ = _get(server, "/uploads/missing.png")
    assert status == 404 and "content-security-policy" in h


def _manifest(root, mutate=None):
    m = {}
    for f in sorted((root / "uploads").iterdir()):
        if f.suffix != ".txt":
            m[f"uploads/{f.name}"] = {"sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "size": f.stat().st_size}
    if mutate:
        mutate(m)
    (root / ".kaisen-uploads.json").write_text(json.dumps(m))


def test_verify_uploads_without_manifest_is_a_no_op(boot, tmp_path):
    assert boot.verify_uploads(str(_site(tmp_path))) == 0


def test_verify_uploads_accepts_a_matching_manifest(boot, tmp_path):
    root = _site(tmp_path)
    (root / "uploads" / "c.txt").unlink()
    _manifest(root)
    assert boot.verify_uploads(str(root)) == 2


def test_verify_uploads_refuses_unlisted_file(boot, tmp_path):
    root = _site(tmp_path)
    _manifest(root)  # c.txt is not listed
    with pytest.raises(boot.BootError, match="not listed"):
        boot.verify_uploads(str(root))


def test_verify_uploads_refuses_hash_mismatch(boot, tmp_path):
    root = _site(tmp_path)
    (root / "uploads" / "c.txt").unlink()
    _manifest(root)
    (root / "uploads" / "b.png").write_bytes(PNG + b"tampered")
    with pytest.raises(boot.BootError, match="does not match"):
        boot.verify_uploads(str(root))


def test_verify_uploads_refuses_disallowed_extension(boot, tmp_path):
    root = _site(tmp_path)
    (root / "uploads" / "c.txt").write_text("<script>")
    _manifest(root, lambda m: m.update({"uploads/c.txt": {"sha256": hashlib.sha256(b"<script>").hexdigest()}}))
    with pytest.raises(boot.BootError, match="file type"):
        boot.verify_uploads(str(root))


def test_verify_uploads_refuses_missing_listed_file_and_bad_manifest(boot, tmp_path):
    root = _site(tmp_path)
    (root / "uploads" / "c.txt").unlink()
    _manifest(root, lambda m: m.update({"uploads/gone.png": {"sha256": "0" * 64}}))
    with pytest.raises(boot.BootError, match="missing"):
        boot.verify_uploads(str(root))
    (root / ".kaisen-uploads.json").write_text("[1]")
    with pytest.raises(boot.BootError, match="valid manifest"):
        boot.verify_uploads(str(root))
    (root / ".kaisen-uploads.json").write_text("{nope")
    with pytest.raises(boot.BootError, match="valid manifest"):
        boot.verify_uploads(str(root))


@pytest.fixture
def public_server(boot, tmp_path):
    import functools
    import http.server
    root = _site(tmp_path)
    (root / "public" / "uploads").mkdir(parents=True)
    (root / "public" / "index.html").write_text("<p>pub</p>")
    (root / "public" / "uploads" / "y.svg").write_bytes(SVG)
    (root / "public" / "uploads" / "a.svg").write_bytes(b"<svg>agent</svg>")
    (root / "secret.txt").write_text("outside")
    handler = functools.partial(boot.StaticHandler, directory=str(root / "public"), uploads_root=str(root / "uploads"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_root_uploads_are_served_when_public_exists(public_server):
    status, h, body = _get(public_server, "/uploads/b.png")
    assert status == 200 and body == PNG and h["content-type"] == "image/png"
    assert h["content-security-policy"] == "default-src 'none'; sandbox" and h["x-content-type-options"] == "nosniff"
    assert _get(public_server, "/index.html")[2] == b"<p>pub</p>"


def test_verified_root_uploads_win_over_public_uploads(public_server):
    status, h, body = _get(public_server, "/uploads/a.svg")
    assert status == 200 and body == SVG and "sandbox" in h["content-security-policy"]


def test_agent_file_in_public_uploads_still_gets_the_csp(public_server):
    status, h, body = _get(public_server, "/uploads/y.svg")
    assert status == 200 and body == SVG and "sandbox" in h["content-security-policy"]


def test_traversal_out_of_uploads_is_blocked(public_server):
    for path in ("/uploads/../secret.txt", "/uploads/%2e%2e/secret.txt", "/uploads/..%2fsecret.txt", "/uploads/../../etc/passwd"):
        status, _h, body = _get(public_server, path)
        assert b"outside" not in body, path
    assert _get(public_server, "/uploads/")[0] == 404


def test_serve_static_passes_root_uploads_only_with_public(boot, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(boot.http.server, "ThreadingHTTPServer", lambda addr, h: type("S", (), {"serve_forever": lambda self: seen.append(h)})())
    root = _site(tmp_path)
    boot.serve_static(str(root), 1)
    assert seen[-1].keywords["uploads_root"] is None
    (root / "public").mkdir()
    boot.serve_static(str(root), 1)
    assert seen[-1].keywords["uploads_root"] == str(root / "uploads")
