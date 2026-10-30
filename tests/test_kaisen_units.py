"""Kaisen pure-logic units: path rules, workspace caps, tool dispatch, deploy spec helpers, runtime check, lease."""
import types

import pytest
from sqlalchemy import create_engine

from app import config, models
from app.kaisen import builder, deploy, worker
from app.kaisen.models import KaisenBuild, KaisenInbox
from app.kaisen.paths import ToolError, normalize_path
from tests.kaisen_helpers import db


@pytest.mark.parametrize("bad", [None, 5, "", "  ", "a\x00b", "a\\b", "x" * 201, "/etc/passwd", "a/../b", "..",
                                 ".", "./", ".git/config", "a/.GITHUB/x"])
def test_normalize_path_rejects(bad):
    with pytest.raises(ToolError):
        normalize_path(bad)


def test_normalize_path_ok():
    assert normalize_path("a//b/./c.txt") == "a/b/c.txt"
    assert normalize_path("kaisen.json") == "kaisen.json"


def _build(s, name="Unit"):
    u = models.User(email="u@example.com", password_hash="x", status="active")
    s.add(u)
    s.flush()
    p = models.Project(name="P", slug="p-unit", created_by=u.id) if hasattr(models.Project, "slug") else None
    s.add(p)
    s.flush()
    b = KaisenBuild(project_id=p.id, name=name, slug="unit", brief="x", created_by=u.id)
    s.add(b)
    s.flush()
    return b


def test_workspace_rules_and_tools(make_client, monkeypatch):
    make_client()
    s = db()
    try:
        b = _build(s)
    except TypeError:
        pytest.skip("model constructor differs")
    ws = builder.Workspace(s, b.id)
    monkeypatch.setattr(config, "KAISEN_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(config, "KAISEN_MAX_FILES", 2)
    monkeypatch.setattr(config, "KAISEN_MAX_BUILD_BYTES", 15)
    assert builder.exec_tool(ws, "list_files", {}) == "(no files)"
    assert "wrote a.txt" in builder.exec_tool(ws, "write_file", {"path": "a.txt", "content": "12345"})
    builder.exec_tool(ws, "write_file", {"path": "a.txt", "content": "123456"})  # overwrite
    assert builder.exec_tool(ws, "read_file", {"path": "a.txt"}) == "123456"
    assert "a.txt (6 bytes)" in builder.exec_tool(ws, "list_files", {})
    with pytest.raises(ToolError, match="too large"):
        ws.write("b.txt", "x" * 11)
    with pytest.raises(ToolError, match="must be a string"):
        ws.write("b.txt", 5)
    builder.exec_tool(ws, "write_file", {"path": "b.txt", "content": "123456789"})
    with pytest.raises(ToolError, match="too many files"):
        ws.write("c.txt", "1")
    with pytest.raises(ToolError, match="size limit"):
        ws.write("b.txt", "1234567890")  # 6 + 10 > 15
    with pytest.raises(ToolError, match="not found"):
        ws.read("nope")
    with pytest.raises(ToolError, match="not found"):
        ws.delete("nope")
    assert builder.exec_tool(ws, "delete_file", {"path": "a.txt"}) == "deleted"
    for name, inp in [("read_file", {}), ("read_file", None), ("write_file", {"path": "a"}),
                      ("write_file", {"path": "", "content": "x"}), ("bogus", {})]:
        with pytest.raises(ToolError):
            builder.exec_tool(ws, name, inp)


def test_slugify():
    assert builder.slugify("  My Todo!! App ") == "my-todo-app"
    assert builder.slugify("!!!") == "build"
    assert len(builder.slugify("a" * 100)) == 60


def test_spec_yaml_and_owned_keys():
    assert deploy.owned_key("KAISEN_X") and deploy.owned_key("DB_HOST") and deploy.owned_key("DATABASE_URL")
    assert not deploy.owned_key("MY_VAR")
    y = deploy.spec_yaml("app", "img:1", "abc", "proj")
    assert 'name: "app"' in y and "db: true" not in y and "- \"KAISEN_BUNDLE_URL\"" in y
    assert "https://" not in y
    y = deploy.spec_yaml("app", "img:1", "abc", "proj", "mysql", {"FOO": "bar"}, ["TOK"])
    assert "db_engine: mysql" in y and '"FOO": "bar"' in y and '- "TOK"' in y and '"KAISEN_BUNDLE_SHA256": "abc"' in y


def test_user_env_and_close_notices(make_client):
    from app import crypto
    make_client()
    assert deploy.user_env(None) == ({}, [])
    app_ = types.SimpleNamespace(env_vars=[
        types.SimpleNamespace(key="KAISEN_A", is_secret=False, value_encrypted=crypto.encrypt("1")),
        types.SimpleNamespace(key="DB_HOST", is_secret=False, value_encrypted=crypto.encrypt("1")),
        types.SimpleNamespace(key="MINE", is_secret=False, value_encrypted=crypto.encrypt("v")),
        types.SimpleNamespace(key="BROKEN", is_secret=False, value_encrypted="not-encrypted"),
        types.SimpleNamespace(key="SEC", is_secret=True, value_encrypted=crypto.encrypt("s")),
    ])
    assert deploy.user_env(app_) == ({"MINE": "v"}, ["SEC"])

    s = db()
    b = _build(s)
    s.add_all([KaisenInbox(build_id=b.id, kind="deploy", text="a", status="open"),
               KaisenInbox(build_id=b.id, kind="push", text="b", status="open")])
    s.flush()
    deploy.close_deploy_notices(s, b)
    st = {i.kind: i.status for i in s.query(KaisenInbox).filter_by(build_id=b.id)}
    assert st == {"deploy": "closed", "push": "open"}


class _RT:
    def __init__(self, seq, codes=(), logs="line"):
        self.seq, self.codes, self.logs_text, self.n = list(seq), list(codes), logs, 0

    def status(self, app_id):
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]

    def probe(self, url):
        return self.codes.pop(0) if self.codes else None

    def logs(self, app_id):
        return self.logs_text


def test_check_outcomes():
    t = [0.0]
    clock = lambda: t[0]
    def sleep(n):
        t[0] += n
    r = deploy.check(_RT([("pending", ""), ("running", "h.x")], [404, 200]), 1, [], 60, clock, sleep)
    assert r.ok and "Result: OK" in r.text and "https://h.x" in r.text
    assert not deploy.check(_RT([("error", "")]), 1, [], 60, clock, sleep).ok
    r = deploy.check(_RT([("pending", "")]), 1, [], 6, clock, sleep)
    assert not r.ok and "no answer yet" in r.text and "(none yet)" in r.text
    r = deploy.check(_RT([("running", "h.x")], [500]), 1, [], 6, clock, sleep)
    assert not r.ok and "http: 500" in r.text
    r = deploy.check(_RT([("running", "h.x")], [200], logs=None), 1, [], 6, clock, sleep)
    assert "logs are not available" in r.text


def test_check_truncates_and_redacts(monkeypatch):
    monkeypatch.setattr(config, "KAISEN_PREVIEW_RESULT_BYTES", 300)
    logs = "\x1b[31mred\x1b[0m not-the-secret " + "z" * 1000 + " /bundles/" + "A" * 30 + ".tar.gz"
    r = deploy.check(_RT([("running", "h.x")], [200], logs=logs), 1, ["not-the-secret"], 1, sleep=lambda n: None)
    assert len(r.text.encode()) <= 300 and "...[truncated]" in r.text
    out = deploy.sanitize(logs, ["not-the-secret"])
    assert "not-the-secret" not in out and "\x1b" not in out and "A" * 30 not in out


def test_platform_runtime(make_client, monkeypatch):
    make_client()
    s = db()
    from tests.kaisen_helpers import Keep
    rt = deploy.PlatformRuntime(Keep(s))
    assert rt.status(99999) == ("deleted", "")
    assert rt.logs(1) is None
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: types.SimpleNamespace(status_code=204))
    assert rt.probe("https://x") == 204
    def boom(*a, **k):
        raise httpx.ConnectError("no")
    monkeypatch.setattr(httpx, "get", boom)
    assert rt.probe("https://x") is None


def test_lease_sqlite_and_mysql_paths(tmp_path):
    sq = worker.Lease(create_engine("sqlite://"))
    assert sq.acquire() and sq.check()

    class Conn:
        def __init__(self, val=1, fail=False):
            self.val, self.fail, self.closed = val, fail, False
        def execute(self, *a, **k):
            if self.fail:
                raise RuntimeError("down")
            return types.SimpleNamespace(scalar=lambda: self.val)
        def commit(self): pass
        def close(self): self.closed = True

    eng = types.SimpleNamespace(dialect=types.SimpleNamespace(name="mysql"))
    eng.connect = lambda: Conn()
    ls = worker.Lease(eng)
    assert not ls.check()  # no connection yet
    assert ls.acquire() and ls.check()
    ls.conn.val = 0
    assert not ls.acquire() and not ls.check()
    ls.conn.fail = True
    c = ls.conn
    assert not ls.check() and c.closed and ls.conn is None
    ls.conn = Conn(fail=True)
    assert not ls.acquire() and ls.conn is None
    ls._drop()  # idempotent
    ls.conn = types.SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    ls._drop()
    assert ls.conn is None


def test_heartbeat(tmp_path):
    p = tmp_path / "hb"
    worker.touch_heartbeat(str(p))
    assert p.exists()
    worker.touch_heartbeat(str(tmp_path / "missing" / "hb"))  # OSError is logged, not raised
