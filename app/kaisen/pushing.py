"""GitHub push of a build's files, shared by the Push endpoint and push-on-finish."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import KaisenBuild, KaisenFile
from .settings import RuntimeSettings


def push_build(db: Session, b: KaisenBuild, rs: RuntimeSettings, github_factory) -> str:
    """Push to the repo in the project's settings and return a message. Raises github.GitHubError."""
    files = [(r.path, r.content) for r in db.scalars(select(KaisenFile).where(KaisenFile.build_id == b.id))]
    if not files:
        return "Nothing to push yet."
    res = github_factory(rs.github_token).push(
        rs.github_repo, files, b.slug, rs.github_branch or None, f"Kaisen: {b.name}", rs.open_pr)
    return (f"Pushed to {res.url}" + (f" (PR: {res.pr_url})" if res.pr_url else "")
            + (" (empty repo was seeded)" if res.seeded else ""))
