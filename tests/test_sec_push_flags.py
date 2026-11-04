"""Review K92-K94: secret-file name filter used by push, LIKE escaping in the admin project search."""
import pytest

from app.kaisen import paths


@pytest.mark.parametrize("p", [".env", "a/.env.local", "prod.env", "deploy/id_rsa", "k/server.PEM", "x/.ENV", "a/b/store.jks"])
def test_secret_names_are_filtered(p):
    assert paths.is_secret_file(p)


@pytest.mark.parametrize("p", ["index.html", "public/env.js", "src/environment.ts", "docs/key-notes.md", "monkey.js"])
def test_ordinary_names_pass(p):
    assert not paths.is_secret_file(p)
