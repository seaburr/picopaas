"""Preview logs for the builder's deploy_preview tool (docs/design/kaisen.md section 6).

The kaisen-worker pod has no cluster credentials (by design), so it cannot read pod logs. The reconciler (API pod, which
has the kube client and already powers the user-facing logs view) snapshots the tail of each preview's logs into
`kaisen_preview_logs` while the build has an active run. The worker only reads that row from the database. Snapshots are
redacted with the build's secrets before they are stored, stripped of control characters, line- and byte-capped, and the
worker redacts again when it hands them to the model. App output is untrusted data."""
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import config, k8s_manifests, models
from .builder import ACTIVE
from .deploy import sanitize
from .models import KaisenBuild, KaisenPreviewLog
from .settings import load_for_build

LOGGED_STATUSES = ("deploying", "running", "error")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clip(lines: list[str]) -> str:
    """Last KAISEN_PREVIEW_LOG_LINES lines, each clipped, whole text capped at KAISEN_PREVIEW_LOG_BYTES from the end."""
    tail = [ln[: config.KAISEN_PREVIEW_LOG_LINE_MAX] for ln in lines[-config.KAISEN_PREVIEW_LOG_LINES:]]
    data = "\n".join(tail).encode()
    if len(data) > config.KAISEN_PREVIEW_LOG_BYTES:
        data = data[-config.KAISEN_PREVIEW_LOG_BYTES:]
    return data.decode(errors="ignore")


def snapshot_secrets(session: Session, app: models.App, build: KaisenBuild) -> list[str]:
    """Every secret value the app could print: the build's own and project secrets, the app's database password and
    bucket keys, and its secret env vars (plus URL-quoted forms, which db.url carries)."""
    from urllib.parse import quote

    from app import crypto

    vals = load_for_build(session, build).secret_values()
    if app.database is not None:
        vals.append(crypto.try_decrypt(app.database.password_encrypted) or "")
    if app.storage is not None:
        vals += [app.storage.access_key, crypto.try_decrypt(app.storage.secret_key_encrypted) or ""]
    vals += [crypto.try_decrypt(v.value_encrypted) or "" for v in app.env_vars if v.is_secret]
    return vals + [quote(v, safe="") for v in vals if v and quote(v, safe="") != v]


def snapshot_preview_logs(session: Session, kube) -> int:
    """Refresh the log snapshot of every preview whose build has an active run; drop snapshots of the rest."""
    rows = session.execute(
        select(models.App, KaisenBuild).join(KaisenBuild, KaisenBuild.id == models.App.kaisen_build_id).where(
            models.App.deleted_at.is_(None), models.App.status.in_(LOGGED_STATUSES),
            KaisenBuild.deleted_at.is_(None), KaisenBuild.status.in_(ACTIVE))).all()
    keep = {app.id for app, _ in rows}
    stale = [r for r in session.scalars(select(KaisenPreviewLog.app_id)) if r not in keep]
    if stale:
        session.execute(delete(KaisenPreviewLog).where(KaisenPreviewLog.app_id.in_(stale)))
    done = 0
    for app, build in rows:
        try:
            result = kube.pod_logs(k8s_manifests.namespace_name(app.id), config.KAISEN_PREVIEW_LOG_LINES)
        except Exception:  # noqa: BLE001 - pod not there yet, API hiccup: keep whatever snapshot exists
            continue
        secrets_ = snapshot_secrets(session, app, build)
        lines = result.lines or ([result.note] if result.note else [])
        # redact each line BEFORE clipping: a cut through the middle of a secret would leave an unmatched prefix of it
        text = clip([sanitize(ln, secrets_) for ln in lines])
        row = session.get(KaisenPreviewLog, app.id)
        if row is None:
            session.add(KaisenPreviewLog(app_id=app.id, text=text, captured_at=_now()))
        else:
            row.text, row.captured_at = text, _now()
        done += 1
    session.commit()
    return done


def read_snapshot(session: Session, app_id: int) -> str | None:
    row = session.get(KaisenPreviewLog, app_id)
    if row is None:
        return None
    session.refresh(row)
    age = max(0, int((_now() - row.captured_at).total_seconds()))
    return f"(snapshot taken {age}s ago)\n{row.text}" if row.text else f"(snapshot taken {age}s ago; the app printed nothing)"
