"""Preview deploys: publish a bundle, then deploy the generic runtime image through the platform's own spec path
(routers/spec.deploy_spec) as the build's creator, into the build's project. No PicoPaaS API key is involved."""
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audit, config, crypto, models, projects
from . import export
from .models import KaisenBuild, KaisenBundle, KaisenFile, KaisenInbox
from .redact import redact

NOT_YET = (404, 502, 503, 504)  # what the edge answers while the app is not up yet
_CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_BUNDLE_URL = re.compile(r"/bundles/[A-Za-z0-9_-]{20,}\.tar\.gz")


class DeployError(Exception):
    """Plain-language message, safe to show a user. `final` = more code changes cannot fix it."""

    def __init__(self, message: str, final: bool = False, status: int = 422):
        super().__init__(message)
        self.final = final
        self.status = status  # HTTP status the API answers with


@dataclass
class CheckResult:
    ok: bool
    text: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_bundle(db: Session, b: KaisenBuild, *, reuse: bool = False) -> tuple[str | None, str, bool]:
    """Store a bundle for the build's current files and keep only the newest (the URL is a bearer capability the app
    holds; older URLs 404). Returns (bundle URL, sha256, unchanged). With reuse=True and identical content nothing is
    minted and the URL is None: the app already holds the live URL (only its hash is stored)."""
    files = [(r.path, r.content) for r in db.scalars(select(KaisenFile).where(KaisenFile.build_id == b.id))]
    if not files:
        raise DeployError("The build has no files yet.")
    data = export.build_tarball(b.slug, files)
    sha = hashlib.sha256(data).hexdigest()
    newest = db.scalars(select(KaisenBundle).where(KaisenBundle.build_id == b.id).order_by(KaisenBundle.id.desc())).first()
    unchanged = newest is not None and newest.sha256 == sha
    if unchanged and reuse:
        return None, sha, True
    token = secrets.token_urlsafe(32)
    db.add(KaisenBundle(build_id=b.id, token_hash=token_hash(token), sha256=sha, data=data))
    db.flush()
    keep = db.scalars(select(KaisenBundle.id).where(KaisenBundle.build_id == b.id).order_by(KaisenBundle.id.desc())
                      .limit(config.KAISEN_BUNDLES_KEPT)).all()
    db.execute(delete(KaisenBundle).where(KaisenBundle.build_id == b.id, KaisenBundle.id.not_in(keep)))
    return f"{config.platform_public_url()}/kaisen/bundles/{token}.tar.gz", sha, unchanged


# A build that declares {"database": "mysql"} in kaisen.json gets its own PicoPaaS MySQL database and these env refs.
DB_ENV = {
    "DB_HOST": "${{ db.host }}", "DB_PORT": "${{ db.port }}", "DB_NAME": "${{ db.name }}", "DB_USER": "${{ db.user }}",
    "DB_PASSWORD": "${{ db.password }}", "DB_CA_CERT": "${{ db.ca_cert }}", "DATABASE_URL": "${{ db.url }}",
}
DATABASES = ("mysql",)


def wanted_database(db: Session, b: KaisenBuild) -> str | None:
    row = db.scalars(select(KaisenFile).where(KaisenFile.build_id == b.id, KaisenFile.path == "kaisen.json")).first()
    try:
        want = (json.loads(row.content) if row else {}).get("database")
    except (ValueError, AttributeError):
        return None  # a broken manifest is reported by the runtime at boot, where the builder can read it
    if want in (None, "", False):
        return None
    if want not in DATABASES:
        raise DeployError(f'kaisen.json "database" must be one of {", ".join(DATABASES)} (got {want!r}).')
    return want


def owned_key(key: str) -> bool:
    """Env keys a Kaisen deploy owns and rewrites; everything else on the app is the user's and is preserved."""
    return key.startswith("KAISEN_") or key.startswith("DB_") or key == "DATABASE_URL"


def spec_yaml(name: str, image: str, sha: str, project_slug: str, database: str | None = None,
              keep_env: dict[str, str] | None = None, keep_secrets: list[str] | None = None) -> str:
    """The bundle URL is a bearer capability, so it goes in as a spec secret (KAISEN_BUNDLE_URL), never in the YAML.
    keep_env / keep_secrets carry the user's own variables (names only for secrets) through the deploy."""
    q = json.dumps  # JSON strings are valid YAML scalars
    out = (f"apiVersion: picopaas/v1\nname: {q(name)}\nimage: {q(image)}\nport: {config.KAISEN_PREVIEW_PORT}\n"
           f"project: {q(project_slug)}\nhealthCheck:\n  enabled: false\n")
    if database:
        out += f"resources:\n  db: true\n  db_engine: {database}\n"
    env = {"KAISEN_BUNDLE_SHA256": sha, **(DB_ENV if database else {}), **(keep_env or {})}
    out += "env:\n" + "".join(f"  {q(k)}: {q(v)}\n" for k, v in env.items())
    out += "secrets:\n" + "".join(f"  - {q(k)}\n" for k in ["KAISEN_BUNDLE_URL", *(keep_secrets or [])])
    return out


def user_env(existing: models.App | None) -> tuple[dict[str, str], list[str]]:
    """The existing app's variables Kaisen does not own: (plain values, secret names). Secret values stay in the
    database; deploy_spec keeps a secret that is declared but not supplied."""
    plain: dict[str, str] = {}
    secret_names: list[str] = []
    if existing is None:
        return plain, secret_names
    for v in existing.env_vars:
        if owned_key(v.key):
            continue
        if v.is_secret:
            secret_names.append(v.key)
        else:
            val = crypto.try_decrypt(v.value_encrypted)
            if val is not None:
                plain[v.key] = val
    return plain, secret_names


def live_app(db: Session, b: KaisenBuild) -> models.App | None:
    if not b.app_id:
        return None
    a = db.get(models.App, b.app_id)
    # project_id must match: an app moved to another project is no longer the build's (its members may differ)
    return a if (a is not None and a.deleted_at is None and a.kaisen_build_id == b.id and a.project_id == b.project_id) else None


def close_deploy_notices(db: Session, b: KaisenBuild) -> None:
    """A deploy that went through makes earlier "deploy failed" inbox items obsolete."""
    (db.query(KaisenInbox).filter(KaisenInbox.build_id == b.id, KaisenInbox.kind == "deploy",
                                  KaisenInbox.status == "open").update({"status": "closed"}, synchronize_session=False))


def deploy_build(db: Session, b: KaisenBuild, registry, *, force: bool = True) -> tuple[models.App, bool]:
    """Deploy (or update) the build's app. Returns (app, changed). Raises DeployError with a plain message."""
    from app.routers.spec import SpecDeploy, deploy_spec  # late: routers import this package

    if not config.KAISEN_RUNTIME_IMAGE:
        raise DeployError("Previews are not available yet: the platform has no Kaisen runtime image configured. "
                          "Ask an administrator.", final=True, status=409)
    if b.deleted_at is not None:
        raise DeployError("This build was deleted.", final=True, status=404)
    creator = db.get(models.User, b.created_by)
    if creator is None or creator.status != "active":
        raise DeployError("The person who created this build can no longer deploy (account not active).", final=True)
    project = db.get(models.Project, b.project_id)
    if project is None or project.deleted_at is not None:
        raise DeployError("The project this build belongs to was deleted.", final=True)
    projects.lock_project(db, project.id)  # serialise the name-clash check and the deploy with other creators
    existing = live_app(db, b)
    name = existing.name if existing is not None else b.name
    clash = (db.query(models.App).filter(models.App.project_id == project.id, models.App.name == name,
                                         models.App.deleted_at.is_(None)).first())
    if clash is not None and clash.kaisen_build_id != b.id:
        db.rollback()
        raise DeployError(f"This project already has an app named {name}. Rename the build and deploy again.", final=True, status=409)
    database = wanted_database(db, b)
    url, sha, unchanged = make_bundle(db, b, reuse=existing is not None and not force)
    if url is None:
        close_deploy_notices(db, b)
        db.commit()
        return existing, False
    keep_env, keep_secrets = user_env(existing)
    yaml_text = spec_yaml(name, config.KAISEN_RUNTIME_IMAGE, sha, project.slug, database, keep_env, keep_secrets)
    try:
        out = deploy_spec(SpecDeploy(spec=yaml_text, secrets={"KAISEN_BUNDLE_URL": url}), db=db, current_user=creator,
                          registry=registry)
    except HTTPException as e:
        db.rollback()
        raise DeployError(str(e.detail), final=e.status_code in (403, 404), status=e.status_code) from None
    app = db.get(models.App, out.id)
    app.kaisen_build_id = b.id
    b.app_id = app.id
    close_deploy_notices(db, b)
    db.commit()
    audit.record(db, creator, "kaisen.deploy", "kaisen_build", b.id, f"app={app.id} created={out.created}")
    return app, True


def sanitize(text: str, secrets_) -> str:
    text = _CONTROL.sub("", text.replace("\r", "\n"))
    return redact(_BUNDLE_URL.sub("/bundles/[REDACTED].tar.gz", text), secrets_)


class Runtime(Protocol):
    def status(self, app_id: int) -> tuple[str, str]: ...  # (app status, public host)
    def probe(self, url: str) -> int | None: ...
    def logs(self, app_id: int) -> str | None: ...


class PlatformRuntime:
    """Worker-side view of a preview: status from the DB (the reconciler writes it), an HTTP probe, no logs.
    TODO(logs): app logs need a path through the platform pods; the worker pod has no cluster credentials."""

    def __init__(self, S):
        self.S = S

    def status(self, app_id: int) -> tuple[str, str]:
        with self.S() as s:
            a = s.get(models.App, app_id)
            if a is None or a.deleted_at is not None:
                return "deleted", ""
            s.refresh(a)
            s.commit()
            return a.status, f"{a.hostname}.{config.APP_DOMAIN}"

    def probe(self, url: str) -> int | None:
        try:
            return httpx.get(url, timeout=8, follow_redirects=False).status_code
        except httpx.HTTPError:
            return None

    def logs(self, app_id: int) -> str | None:
        return None


def check(runtime: Runtime, app_id: int, secrets_, wait_s: float, clock=time.monotonic, sleep=time.sleep) -> CheckResult:
    """Poll until the app is running and answers HTTP (or errored, or wait_s passed). Text is safe to show a model."""
    deadline = clock() + wait_s
    code = None
    while True:
        status, host = runtime.status(app_id)
        if status in ("error", "deleted", "suspended"):
            break
        if status == "running" and host:
            code = runtime.probe(f"https://{host}")
            if code is not None and code not in NOT_YET:
                break
        if clock() >= deadline:
            break
        sleep(5)
    logs = runtime.logs(app_id) or "(logs are not available yet)"
    ok = status == "running" and code is not None and 200 <= code < 300
    head = (f"Result: {'OK' if ok else 'NOT OK'}\nstatus: {status}\nurl: {'https://' + host if host else '(none yet)'}\n"
            f"http: {code if code is not None else 'no answer yet'}\nlast log lines:\n")
    head = sanitize(head, secrets_)
    room = config.KAISEN_PREVIEW_RESULT_BYTES - len(head.encode())
    body = sanitize(logs, secrets_).encode()
    if len(body) > room:
        body = b"...[truncated]\n" + body[-(room - 15):]
    return CheckResult(ok, head + body.decode(errors="ignore"))
