"""Per-project Kaisen settings. Secrets are Fernet-encrypted (app.crypto) and write-only."""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app import config, crypto
from .models import KaisenBuild, KaisenSettings

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
    build_token: str = ""  # the build's own GitHub token, only set by load_for_build (kept for redaction)

    def secret_values(self) -> list[str]:
        return [self.anthropic_api_key, self.github_token, self.build_token, config.SECRET_KEY, config.FERNET_KEY]


@dataclass
class Output:
    """A build's output settings with the token it will use already chosen. Never serialised to a client."""
    mode: str = "download"
    repo: str = ""
    branch: str = ""
    push_on_finish: bool = True
    open_pr: bool = False
    auth: str = "project"
    token: str = ""  # the selected token (project's or the build's)

    @property
    def push_enabled(self) -> bool:
        return self.mode == "github" and bool(self.repo and self.token)


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
    )


def _build_token(b: KaisenBuild) -> str:
    return (crypto.try_decrypt(b.github_token_enc) or "") if b.github_token_enc else ""


def load_for_build(db: Session, b: KaisenBuild) -> RuntimeSettings:
    """Project settings plus the build's own token (so it is redacted everywhere the project secrets are)."""
    rs = load(db, b.project_id)
    rs.build_token = _build_token(b)
    return rs


def output_for(b: KaisenBuild, rs: RuntimeSettings) -> Output:
    token = _build_token(b) if b.github_auth == "build" else rs.github_token
    return Output(mode=b.output_mode, repo=b.github_repo, branch=b.github_branch, push_on_finish=b.push_on_finish,
                  open_pr=b.open_pr, auth=b.github_auth, token=token)


def describe_output(b: KaisenBuild, rs: RuntimeSettings) -> dict:
    """Section 10 shape of the build detail's `output`. Tokens: only {set, last4}."""
    out = output_for(b, rs)
    return {"mode": out.mode, "repo": out.repo, "branch": out.branch, "push_on_finish": out.push_on_finish,
            "open_pr": out.open_pr, "auth": out.auth, "token": _secret_view(_build_token(b)),
            "project_token_set": bool(rs.github_token), "can_push": out.push_enabled}


def validate_branch(branch: str) -> None:
    if branch and (not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch or branch.startswith(("/", "-"))
                   or branch.endswith(("/", ".lock")) or "//" in branch):
        raise ValueError("The branch name has characters GitHub does not allow.")


def apply_output(b: KaisenBuild, rs: RuntimeSettings, data: dict) -> None:
    """Validate `data` (only the fields the client sent) against the build and set the columns. No commit.
    Raises ValueError with a plain-language message; the build is untouched when it raises."""
    mode = data.get("mode", b.output_mode)
    if mode not in config.KAISEN_OUTPUT_MODES:
        raise ValueError("Output must be download or github.")
    auth = data.get("auth", b.github_auth)
    if auth not in config.KAISEN_GITHUB_AUTHS:
        raise ValueError("Token choice must be project or build.")
    repo_in = data["repo"] if "repo" in data else b.github_repo
    repo = parse_repo(repo_in) if repo_in else ""
    if repo_in and not repo:
        raise ValueError("The GitHub repository must look like https://github.com/owner/repo.")
    branch = ((data["branch"] if "branch" in data else b.github_branch) or "").strip()
    validate_branch(branch)
    token = _build_token(b)
    if data.get("clear_github_token") or auth == "project":
        token = ""
    if (data.get("github_token") or "").strip():
        if auth != "build":
            raise ValueError("A token for this build only can be saved when 'Use a token for this build only' is chosen.")
        token = data["github_token"].strip()
    if mode == "github":
        if not repo:
            raise ValueError("Pushing to GitHub needs the repository URL.")
        if auth == "project" and not rs.github_token:
            raise ValueError("This project has no GitHub token yet. A project owner can add one in Kaisen settings, "
                             "or choose a token for this build only.")
        if auth == "build" and not token:
            raise ValueError("Pushing to GitHub needs an access token for this build. Paste one, or use the project's token.")
    b.output_mode, b.github_repo, b.github_branch, b.github_auth = mode, repo, branch, auth
    b.push_on_finish = bool(data.get("push_on_finish", b.push_on_finish))
    b.open_pr = bool(data.get("open_pr", b.open_pr))
    b.github_token_enc = crypto.encrypt(token) if token else None


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
        "github_token": _secret_view(rs.github_token),
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
    if row is None:
        row = KaisenSettings(project_id=project_id)
        db.add(row)
    row.anthropic_key_enc = crypto.encrypt(key) if key else None
    row.github_token_enc = crypto.encrypt(token) if token else None
    row.model, row.effort = model, effort
    row.cap_run, row.cap_day, row.cap_build = caps["cap_run"], caps["cap_day"], caps["cap_build"]
    row.updated_by = user_id
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()


def changed_fields(data: dict) -> list[str]:
    """Names (never values) of the fields in a save, for the audit detail."""
    return sorted(k for k in data if data[k] is not None)
