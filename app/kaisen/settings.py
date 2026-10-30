"""Per-project Kaisen settings. Secrets are Fernet-encrypted (app.crypto) and write-only."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app import config, crypto
from .models import KaisenSettings

_REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")


@dataclass
class RuntimeSettings:
    """Decrypted view used by the worker and the push/test paths. Never serialised to a client."""
    project_id: int = 0
    anthropic_api_key: str = ""
    github_token: str = ""
    model: str = config.KAISEN_DEFAULT_MODEL
    effort: str = config.KAISEN_DEFAULT_EFFORT
    cap_run: float = config.KAISEN_DEFAULT_CAP_RUN
    cap_day: float = config.KAISEN_DEFAULT_CAP_DAY
    cap_build: float = config.KAISEN_DEFAULT_CAP_BUILD
    github_repo: str = ""
    github_branch: str = ""
    output_mode: str = "download"
    push_on_finish: bool = True
    open_pr: bool = False

    @property
    def push_enabled(self) -> bool:
        return self.output_mode == "github" and bool(self.github_repo and self.github_token)

    def secret_values(self) -> list[str]:
        return [self.anthropic_api_key, self.github_token, config.SECRET_KEY, config.FERNET_KEY]


def parse_repo(text: str) -> str:
    """'https://github.com/owner/repo(.git)' or 'owner/repo' -> 'owner/repo'; '' if it is neither."""
    m = _REPO_RE.match(text.strip())
    if not m or m.group(2) in (".", ".."):
        return ""
    return f"{m.group(1)}/{m.group(2)}"


def load(db: Session, project_id: int) -> RuntimeSettings:
    row = db.get(KaisenSettings, project_id)
    if row is None:
        return RuntimeSettings(project_id=project_id)
    return RuntimeSettings(
        project_id=project_id,
        anthropic_api_key=(crypto.try_decrypt(row.anthropic_key_enc) or "") if row.anthropic_key_enc else "",
        github_token=(crypto.try_decrypt(row.github_token_enc) or "") if row.github_token_enc else "",
        model=row.model, effort=row.effort, cap_run=row.cap_run, cap_day=row.cap_day, cap_build=row.cap_build,
        github_repo=row.github_repo, github_branch=row.github_branch, output_mode=row.output_mode,
        push_on_finish=row.push_on_finish, open_pr=row.open_pr,
    )


def _secret_view(value: str) -> dict:
    return {"set": bool(value), "last4": value[-4:] if len(value) >= 8 else ""}


def describe(db: Session, project_id: int, can_edit: bool) -> dict:
    rs = load(db, project_id)
    row = db.get(KaisenSettings, project_id)
    return {
        "project_id": project_id,
        "anthropic_key": _secret_view(rs.anthropic_api_key),
        "has_anthropic_key": bool(rs.anthropic_api_key),
        "model": rs.model, "effort": rs.effort,
        "models": list(config.KAISEN_PRICES), "efforts": list(config.KAISEN_EFFORTS),
        "cap_run": rs.cap_run, "cap_day": rs.cap_day, "cap_build": rs.cap_build,
        "output_mode": rs.output_mode, "github_repo": rs.github_repo, "github_branch": rs.github_branch,
        "github_token": _secret_view(rs.github_token),
        "push_on_finish": rs.push_on_finish, "open_pr": rs.open_pr,
        "can_edit": can_edit,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
    }


def save(db: Session, project_id: int, user_id: int, data: dict) -> None:
    """Validate and store. `data` holds only the fields the client sent; secrets omitted/blank = keep.
    Raises ValueError with a plain-language message."""
    row = db.get(KaisenSettings, project_id)
    cur = load(db, project_id)
    model = data.get("model", cur.model)
    if model not in config.KAISEN_PRICES:
        raise ValueError(f"Unknown model. Choose one of: {', '.join(config.KAISEN_PRICES)}.")
    effort = data.get("effort", cur.effort)
    if effort not in config.KAISEN_EFFORTS:
        raise ValueError(f"Unknown effort. Choose one of: {', '.join(config.KAISEN_EFFORTS)}.")
    caps = {}
    for k in ("cap_run", "cap_day", "cap_build"):
        v = data.get(k, getattr(cur, k))
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= config.KAISEN_CAP_MAX:
            raise ValueError(f"{k} must be a number between 0 and {config.KAISEN_CAP_MAX:g} (US dollars).")
        caps[k] = float(v)
    mode = data.get("output_mode", cur.output_mode)
    if mode not in config.KAISEN_OUTPUT_MODES:
        raise ValueError("Output must be download or github.")
    repo_in = data.get("github_repo", cur.github_repo)
    repo = parse_repo(repo_in) if repo_in else ""
    if repo_in and not repo:
        raise ValueError("The GitHub repository must look like https://github.com/owner/repo.")
    branch = (data.get("github_branch", cur.github_branch) or "").strip()
    if branch and (not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch or branch.startswith(("/", "-"))
                   or branch.endswith(("/", ".lock")) or "//" in branch):
        raise ValueError("The branch name has characters GitHub does not allow.")

    key = cur.anthropic_api_key
    if data.get("clear_anthropic_key"):
        key = ""
    elif (data.get("anthropic_key") or "").strip():
        key = data["anthropic_key"].strip()
    token = cur.github_token
    if data.get("clear_github_token"):
        token = ""
    elif (data.get("github_token") or "").strip():
        token = data["github_token"].strip()
    if mode == "github" and not repo:
        raise ValueError("Pushing to GitHub needs the repository URL.")
    if mode == "github" and not token:
        raise ValueError("Pushing to GitHub needs an access token.")

    if row is None:
        row = KaisenSettings(project_id=project_id)
        db.add(row)
    row.anthropic_key_enc = crypto.encrypt(key) if key else None
    row.github_token_enc = crypto.encrypt(token) if token else None
    row.model, row.effort = model, effort
    row.cap_run, row.cap_day, row.cap_build = caps["cap_run"], caps["cap_day"], caps["cap_build"]
    row.github_repo, row.github_branch, row.output_mode = repo, branch, mode
    row.push_on_finish = bool(data.get("push_on_finish", cur.push_on_finish))
    row.open_pr = bool(data.get("open_pr", cur.open_pr))
    row.updated_by = user_id
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()


def changed_fields(data: dict) -> list[str]:
    """Names (never values) of the fields in a save, for the audit detail."""
    return sorted(k for k in data if data[k] is not None)
