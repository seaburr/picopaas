"""app/env_refs.py::resolve — substituting `${{ ref }}` placeholders with real values."""
import pytest

from app.env_refs import EnvValidationError, resolve


def test_resolve_substitutes_known_refs():
    env = {"DATABASE_URL": "${{ db.url }}", "LOG_LEVEL": "info"}
    refs = {"db.url": "mysql://app_1:pw@mysql:3306/app_1"}
    resolved = resolve(env, refs)
    assert resolved == {"DATABASE_URL": "mysql://app_1:pw@mysql:3306/app_1", "LOG_LEVEL": "info"}


def test_resolve_leaves_literals_untouched():
    env = {"FOO": "bar", "BAZ": "${{ storage.bucket }}"}
    refs = {"storage.bucket": "pp-7"}
    resolved = resolve(env, refs)
    assert resolved["FOO"] == "bar"
    assert resolved["BAZ"] == "pp-7"


def test_resolve_unknown_ref_raises():
    env = {"DATABASE_URL": "${{ db.url }}"}
    with pytest.raises(EnvValidationError):
        resolve(env, refs={})


def test_resolve_empty_env_returns_empty():
    assert resolve({}, {"db.url": "x"}) == {}
