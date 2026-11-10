"""Kaisen runtime: the bundle archive and kaisen.json are checked before anything runs. The platform builds the bundle
itself, so these guards are defence in depth, but nothing else tested their rejections."""
import importlib.util
import io
import os
import tarfile
from pathlib import Path

import pytest

BOOT = Path(__file__).resolve().parent.parent / "images" / "kaisen-runtime" / "boot.py"


@pytest.fixture()
def boot():
    spec = importlib.util.spec_from_file_location("kaisen_boot_extract", BOOT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _bundle(path, entries):
    """entries: (name, kind, payload) with kind in file | dir | symlink | hardlink | fifo."""
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE}[kind]
            if kind in ("symlink", "hardlink"):
                info.linkname = payload
            tar.addfile(info)
    return str(path)


def test_a_plain_bundle_extracts_files_only_and_without_exec_bits(boot, tmp_path):
    tgz = _bundle(tmp_path / "b.tgz", [("site", "dir", None), ("site/index.html", "file", b"<h1>hi</h1>"),
                                       ("site/js/app.js", "file", b"1")])
    dest = tmp_path / "out"
    assert boot.safe_extract(tgz, str(dest)) == 2
    assert (dest / "site" / "index.html").read_bytes() == b"<h1>hi</h1>"
    assert oct((dest / "site" / "js" / "app.js").stat().st_mode & 0o777) == "0o644"
    assert boot.project_root(str(dest)) == str(dest / "site")  # one top-level folder is the project root


@pytest.mark.parametrize("entry,match", [
    (("/etc/cron.d/x", "file", b"x"), "unsafe path"),
    (("site/../../x", "file", b"x"), "unsafe path"),
    (("site\\x", "file", b"x"), "unsafe path"),
    (("site/link", "symlink", "/etc/passwd"), "links/devices"),
    (("site/hard", "hardlink", "site/index.html"), "links/devices"),
    (("site/pipe", "fifo", None), "links/devices"),
])
def test_hostile_entries_are_rejected_before_anything_is_written(boot, tmp_path, entry, match):
    tgz = _bundle(tmp_path / "b.tgz", [("site/index.html", "file", b"ok"), entry])
    dest = tmp_path / "out"
    with pytest.raises(boot.BootError, match=match):
        boot.safe_extract(tgz, str(dest))
    assert not any(files for _, _, files in os.walk(dest))  # the good file before it was not written either


def test_a_bundle_that_is_too_large_when_extracted_is_rejected(boot, tmp_path, monkeypatch):
    monkeypatch.setattr(boot, "MAX_EXTRACT_BYTES", 10)
    tgz = _bundle(tmp_path / "b.tgz", [("a", "file", b"123456"), ("b", "file", b"123456")])
    with pytest.raises(boot.BootError, match="too large"):
        boot.safe_extract(tgz, str(tmp_path / "out"))


def test_sha_mismatch_is_rejected(boot, tmp_path):
    f = tmp_path / "b.tgz"
    f.write_bytes(b"bundle")
    boot.verify_sha(str(f), boot.sha256_file(str(f)).upper() + "\n")  # case and whitespace do not matter
    with pytest.raises(boot.BootError, match="sha256 mismatch"):
        boot.verify_sha(str(f), "0" * 64)


@pytest.mark.parametrize("text,match", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "must be a JSON object"),
    ('{"runtime": "ruby"}', "runtime must be one of"),
    ('{"runtime": "node", "start": 5}', "must be strings"),
    ('{"runtime": "python"}', "needs a 'start' command"),
    ('{"runtime": "python", "start": "   "}', "needs a 'start' command"),
])
def test_bad_manifests_are_rejected_with_a_reason(boot, text, match):
    with pytest.raises(boot.BootError, match=match):
        boot.parse_manifest(text)


def test_manifest_defaults(boot):
    assert boot.parse_manifest('{"runtime": "node"}')["start"] == "npm start"
    assert boot.parse_manifest('{"runtime": "static", "health": "", "test": null}') == {
        "runtime": "static", "start": "", "health": "/", "test": ""}
