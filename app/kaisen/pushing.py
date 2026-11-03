"""GitHub push of a build's files, shared by the Push endpoint and push-on-finish."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import uploads
from .models import KaisenBuild, KaisenFile
from .settings import Output


def push_build(db: Session, b: KaisenBuild, out: Output, github_factory) -> str:
    """Push to the repo in the build's output settings and return a message. Raises github.GitHubError."""
    files: list[tuple[str, str | bytes]] = [(r.path, r.content) for r in db.scalars(select(KaisenFile).where(KaisenFile.build_id == b.id))]
    if not files:
        return "Nothing to push yet."
    files += uploads.asset_blobs(db, b.id)  # uploaded images go as they are; references never leave the platform
    res = github_factory(out.token).push(
        out.repo, files, b.slug, out.branch or None, f"Kaisen: {b.name}", out.open_pr)
    return (f"Pushed to {res.url}" + (f" (PR: {res.pr_url})" if res.pr_url else "")
            + (" (empty repo was seeded)" if res.seeded else ""))
