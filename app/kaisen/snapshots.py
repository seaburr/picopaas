"""Restore points (design s15 H5): snapshot a build's files at each run start and before a restore; restore; diff.

A snapshot is zlib-compressed JSON {path: content}. Identical file sets are not stored twice (sha256 of a canonical
sorted listing is compared with the newest snapshot). Restoring never deploys: the usual stale detection
(httptool.files_changed, deploy.make_bundle) sees the changed files and a later deploy builds a new bundle."""
import difflib
import hashlib
import json
import logging
import time
import zlib

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import config
from .paths import ToolError, normalize_path
from .models import KaisenFile, KaisenRun, KaisenSnapshot
from .redact import redact

log = logging.getLogger("picopaas.kaisen")


def current_files(s: Session, build_id: int) -> dict[str, str]:
    return {r.path: r.content for r in s.scalars(select(KaisenFile).where(KaisenFile.build_id == build_id))}


def fingerprint(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(files.items()), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def newest(s: Session, build_id: int) -> KaisenSnapshot | None:
    return s.scalars(select(KaisenSnapshot).where(KaisenSnapshot.build_id == build_id)
                     .order_by(KaisenSnapshot.id.desc())).first()


def take(s: Session, build_id: int, kind: str, run_id: int | None = None) -> tuple[KaisenSnapshot | None, bool]:
    """Snapshot the current files. Returns (snapshot, created): an unchanged file set returns (newest, False); an empty
    set or an oversized snapshot returns (None, False). Prunes to KAISEN_SNAPSHOTS_KEPT."""
    files = current_files(s, build_id)
    if not files:
        return None, False
    sha = fingerprint(files)
    last = newest(s, build_id)
    if last is not None and last.sha256 == sha:
        return last, False
    blob = zlib.compress(json.dumps(files, ensure_ascii=False).encode(), 6)
    if len(blob) > config.KAISEN_SNAPSHOT_MAX_BYTES:
        log.warning("kaisen snapshot skipped for build %s: %s bytes compressed exceeds the cap", build_id, len(blob))
        return None, False
    snap = KaisenSnapshot(build_id=build_id, run_id=run_id, kind=kind, files_count=len(files),
                          bytes=sum(len(c.encode()) for c in files.values()), sha256=sha, blob=blob)
    s.add(snap)
    s.flush()
    keep = s.scalars(select(KaisenSnapshot.id).where(KaisenSnapshot.build_id == build_id)
                     .order_by(KaisenSnapshot.id.desc()).limit(config.KAISEN_SNAPSHOTS_KEPT)).all()
    s.execute(delete(KaisenSnapshot).where(KaisenSnapshot.build_id == build_id, KaisenSnapshot.id.not_in(keep)))
    return snap, True


def take_safely(s: Session, build_id: int, kind: str, run_id: int | None = None) -> None:
    """take() for run starts: a storage problem (e.g. packet size) must never stop the run itself."""
    try:
        with s.begin_nested():
            take(s, build_id, kind, run_id)
    except Exception:
        log.exception("kaisen snapshot failed for build %s", build_id)


class SnapshotError(Exception):
    """A stored snapshot that does not look like something take() would have written."""


# JSON escaping can expand control characters up to 6x; anything past this is not one of ours.
_MAX_DECOMPRESSED = 6 * config.KAISEN_MAX_BUILD_BYTES + 64 * 1024


def load(snap: KaisenSnapshot) -> dict[str, str]:
    """Decompress with a hard output bound (no zlib bomb), then re-apply the workspace rules: normalized unique paths,
    string contents, file count, per-file and total size. Defence in depth: blobs are ours, but a restore rewrites files."""
    d = zlib.decompressobj()
    try:
        raw = d.decompress(snap.blob, _MAX_DECOMPRESSED)
        if d.unconsumed_tail or not d.eof:
            raise SnapshotError("restore point is too large")
        files = json.loads(raw.decode())
    except SnapshotError:
        raise
    except (zlib.error, ValueError, UnicodeDecodeError) as e:
        raise SnapshotError("restore point is damaged") from e
    if not isinstance(files, dict) or len(files) > config.KAISEN_MAX_FILES:
        raise SnapshotError("restore point is damaged")
    out, total = {}, 0
    for path, content in files.items():
        try:
            ok = isinstance(content, str) and normalize_path(path) == path
        except ToolError:
            ok = False
        size = len(content.encode(errors="replace")) if isinstance(content, str) else 0
        total += size
        if not ok or size > config.KAISEN_MAX_FILE_BYTES or total > config.KAISEN_MAX_BUILD_BYTES:
            raise SnapshotError("restore point is damaged")
        out[path] = content
    return out


def listing(s: Session, build_id: int) -> list[KaisenSnapshot]:
    return list(s.scalars(select(KaisenSnapshot).where(KaisenSnapshot.build_id == build_id)
                          .order_by(KaisenSnapshot.id.desc())))


def run_numbers(s: Session, build_id: int) -> dict[int, int]:
    """run id -> 1-based position of that run in the build."""
    ids = s.scalars(select(KaisenRun.id).where(KaisenRun.build_id == build_id).order_by(KaisenRun.id)).all()
    return {rid: i for i, rid in enumerate(ids, 1)}


def runs_with_diff(s: Session, build_id: int) -> set[int]:
    return {r for r in s.scalars(select(KaisenSnapshot.run_id).where(
        KaisenSnapshot.build_id == build_id, KaisenSnapshot.run_id.is_not(None)))}


def restore(s: Session, build_id: int, snap: KaisenSnapshot) -> tuple[int, int | None]:
    """Replace the build's files with the snapshot's. Takes a before_restore snapshot first (None when there is
    nothing to keep; the existing newest one when the files are unchanged since). Caller checks no run is active."""
    files = load(snap)  # validate first: a damaged blob must not cost the current files or add a snapshot
    before, _ = take(s, build_id, "before_restore")
    s.execute(delete(KaisenFile).where(KaisenFile.build_id == build_id))
    for path, content in sorted(files.items()):
        s.add(KaisenFile(build_id=build_id, path=path, content=content, size=len(content.encode())))
    s.flush()
    return len(files), before.id if before else None


def diff(old: dict[str, str], new: dict[str, str], secrets=()) -> dict:
    """Added/removed/changed paths between two file sets; contents are redacted before diffing, so a secret never
    shows. Per-file and total size caps keep the response small."""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed, total, truncated = [], 0, False
    deadline = time.monotonic() + config.KAISEN_DIFF_BUDGET_SECONDS  # difflib is quadratic on many repeated lines
    for path in sorted(p for p in set(old) & set(new) if old[p] != new[p]):
        if total >= config.KAISEN_DIFF_MAX_BYTES or time.monotonic() > deadline:
            truncated = True
            changed.append({"path": path, "diff": ""})
            continue
        a = redact(old[path], secrets).splitlines(keepends=True)
        b = redact(new[path], secrets).splitlines(keepends=True)
        if max(len(a), len(b)) > config.KAISEN_DIFF_MAX_LINES:
            truncated = True
            changed.append({"path": path, "diff": "... file has too many lines to diff here\n"})
            continue
        text = "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n"
                       for ln in difflib.unified_diff(a, b, f"a/{path}", f"b/{path}"))
        if len(text.encode()) > config.KAISEN_DIFF_FILE_MAX_BYTES:
            text = text.encode()[:config.KAISEN_DIFF_FILE_MAX_BYTES].decode(errors="ignore") + "\n... diff truncated\n"
        total += len(text.encode())
        changed.append({"path": path, "diff": text})
    return {"added": added, "removed": removed, "changed": changed, "truncated": truncated}
