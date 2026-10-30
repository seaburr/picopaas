"""Shared fakes for the Kaisen tests: no network, no unittest.mock patching of internals."""
from app import config, models
from app.db import get_db
from app.kaisen.github import FakeGitHub
from app.kaisen.llm import FakeModelClient, ModelError
from app.main import app as fastapi_app

KEY = "not-the-anthropic-key"
TOKEN = "not-the-github-token"


class Keep:
    """Session factory over the shared test session; like `with SessionLocal() as s`, an exit drops uncommitted work."""

    def __init__(self, s):
        self.s = s

    def __call__(self):
        return self

    def __enter__(self):
        return self.s

    def __exit__(self, *exc):
        self.s.rollback()
        self.s.expire_all()


def db():
    return next(fastapi_app.dependency_overrides[get_db]())


class FakeRuntime:
    """Stands in for the reconciler + edge: a pending app becomes running on the first poll."""

    def __init__(self, code=200, logs="booted ok", stay=None):
        self.code, self.logs_text, self.stay = code, logs, stay
        self.probes = []

    def status(self, app_id):
        s = db()
        a = s.get(models.App, app_id)
        s.refresh(a)
        if self.stay:
            a.status = self.stay
        elif a.status in ("pending", "deploying"):
            a.status = "running"
        s.commit()
        return a.status, f"{a.hostname}.{config.APP_DOMAIN}"

    def probe(self, url):
        self.probes.append(url)
        return self.code

    def logs(self, app_id):
        return self.logs_text


class FakeAnthropic:
    def __init__(self, ok=True):
        self.ok = ok

    def retrieve_model(self, model):
        if not self.ok:
            raise ModelError("Anthropic rejected the API key (401)")
        return model


class Fakes:
    def __init__(self):
        self.github = FakeGitHub()
        self.tokens = []  # the token each GitHub client was built with
        self.anthropic = FakeAnthropic()
        self.model = None


def app_env(app_id):
    """Decrypted env of an app straight from the database: {key: (value, is_secret)}."""
    from app import crypto
    s = db()
    s.expire_all()
    a = s.get(models.App, app_id)
    return {v.key: (crypto.decrypt(v.value_encrypted), v.is_secret) for v in a.env_vars}
