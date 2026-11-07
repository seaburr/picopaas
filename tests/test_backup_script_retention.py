"""The backup image's retention step: MODE=backup clears stale restic locks first, and a failing `forget --prune` reports a
one fixed phrase (never restic's text) instead of discarding stderr. The Python side matches the
"retention cleanup failed" prefix by substring, so the reason is appended after a colon."""
import os
import stat
import subprocess

import pytest

from tests.test_backups_security import _run, _script_env


def _restic(tmp_path, calls, forget_stderr):
    """Replace the stubbed restic: forget fails and writes `forget_stderr` to stderr; everything else succeeds."""
    p = tmp_path / "bin" / "restic"
    p.write_text(
        "#!/bin/sh\n"
        f'echo "restic $@" >> {calls}\n'
        'for a in "$@"; do case "$a" in\n'
        '  backup) echo \'{"message_type":"summary","snapshot_id":"cafe0123456789","data_added":1}\'; exit 0;;\n'
        '  stats) echo \'{"total_size":1}\'; exit 0;;\n'
        f'  forget) cat <<\'EOF\' >&2\n{forget_stderr}\nEOF\n    exit 1;;\n'
        "esac; done\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def _db(tmp_path):
    """The shared env, switched to a db backup with a stub mariadb-dump (the bucket kind needs a consistent object copy)."""
    env, calls = _script_env(tmp_path, ["a.txt"])
    p = tmp_path / "bin" / "mariadb-dump"
    p.write_text("#!/bin/sh\necho '-- dump'\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    env.update(KIND="db", ENGINE="mysql", DB_HOST="h", DB_NAME="n", DB_USER="u", DB_PASSWORD="not-the-password")
    return env, calls


def _env(tmp_path, stderr):
    env, calls = _db(tmp_path)
    _restic(tmp_path, calls, stderr)
    return env, calls


def test_backup_unlocks_stale_locks_before_retention_without_remove_all(tmp_path):
    env, calls = _db(tmp_path)
    r = _run(env, tmp_path)
    assert r.returncode == 0, r.stdout
    lines = calls.read_text().splitlines()
    unlock = [i for i, line in enumerate(lines) if line.startswith("restic unlock")]
    forget = [i for i, line in enumerate(lines) if " forget " in line]
    assert unlock and forget and unlock[0] < forget[0]
    assert all("--remove-all" not in line for line in lines)


PHRASES = {"repository locked", "access denied", "connection failed", "repository not found", "out of space", "other"}
SCRIPT = os.path.join(os.path.dirname(__file__), "..", "images", "backup", "picopaas-backup.sh")


def _phrase(tmp_path, stderr):
    env, _ = _env(tmp_path, stderr)
    r = _run(env, tmp_path)
    assert r.returncode == 1
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith("PICOPAAS_ERROR retention cleanup failed: ")
    return last.split("failed: ", 1)[1]


@pytest.mark.parametrize("stderr,phrase", [
    ("Fatal: unable to create lock in backend: repository is already locked by PID 7 on pod-1 by backup", "repository locked"),
    ('Fatal: Get "https://b.nyc3.example.com/": dial tcp 10.1.2.3:443: connect: connection refused', "connection failed"),
    ("Fatal: lookup nyc3.example.com on 10.245.0.10:53: no such host", "connection failed"),
    ("Fatal: tls: failed to verify certificate: x509: certificate signed by unknown authority", "connection failed"),
    ("Fatal: unable to open repository: AccessDenied: Access Denied. status code: 403", "access denied"),
    ("Fatal: wrong password or no key found", "access denied"),
    ("Fatal: unable to open config file: Stat: The specified key does not exist.", "repository not found"),
    ("Fatal: repository does not exist: unable to open config file", "repository not found"),
    ("Fatal: write /repo/data/ab/cd: no space left on device", "out of space"),
    ("Fatal: something entirely unexpected", "other"),
])
def test_reason_is_one_fixed_phrase_per_known_restic_error(tmp_path, stderr, phrase):
    assert _phrase(tmp_path, stderr) == phrase


def test_reason_looks_at_every_line_not_just_the_first(tmp_path):
    assert _phrase(tmp_path, "Fatal: harmless header\nlock was created at 2026-11-07\nrepository is already locked by PID 2") == "repository locked"


@pytest.mark.parametrize("stderr", [
    "Fatal: unable to open repository at s3:https://nyc3.example.com/bucket/platform/db: Access Denied",
    'Fatal: Get "https://bucket.nyc3.example.com/x?X-Amz-Signature=abcdef0123456789&X-Amz-Credential=AKIAEXAMPLE": dial tcp 10.9.8.7:3900: timeout',
    "Fatal: wrong password or no key found for not-the-password",
    "Fatal: AWS_SECRET_ACCESS_KEY=not-the-backup-secret rejected, key backup-key-id, host 192.168.1.20 bucket my-private-bucket",
    "Fatal: signature mismatch for DO00ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
    "Fatal: cted redacted <redacted> red",
])
def test_reason_can_only_be_a_fixed_phrase_never_restic_text(tmp_path, stderr):
    env, _ = _env(tmp_path, stderr)
    env["DB_PASSWORD"] = "cted"  # K124: a secret that is a substring of the old replacement text must not hang the script
    env["AWS_SECRET_ACCESS_KEY"] = "dact"
    r = subprocess.run(["sh", SCRIPT], env=env, capture_output=True, text=True, cwd=tmp_path, timeout=30)
    out = r.stdout + r.stderr
    assert r.returncode == 1
    assert out.strip().splitlines()[-1].split("failed: ", 1)[1] in PHRASES
    for leak in ("https://", "s3:", "nyc3", "X-Amz", "AKIA", "abcdef0123", "not-the-password", "not-the-backup-secret", "backup-key-id",
                 "DO00ABCDEFGH", "platform/db", "10.9.8.7", "192.168", "my-private-bucket", "redacted"):
        assert leak not in out, leak


def test_failed_retention_without_stderr_keeps_the_plain_message(tmp_path):
    env, _ = _env(tmp_path, "")
    r = _run(env, tmp_path)
    assert r.returncode == 1
    assert r.stdout.strip().splitlines()[-1] == "PICOPAAS_ERROR retention cleanup failed"
