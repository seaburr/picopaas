"""Kaisen quality-pass probes (reviewer E): regression pins for flows found by reading the code."""
from app import models
from app.kaisen.llm import FakeModelClient, reply, tool_use
from tests.test_suspensions import _db, _last_run, _run, _uid, go, k, team  # noqa: F401  (fixtures)


def test_run_creator_rejected_mid_run_says_rejected_not_suspended(k):  # noqa: F811
    _run(k.a, k.bid)
    uid = _uid("a@example.com")

    def reject(_m):
        s = _db()
        s.get(models.User, uid).status = "rejected"
        s.commit()
        return reply(tool_use("finish", {"summary": "done"}))

    go(FakeModelClient([reject, reply(tool_use("finish", {"summary": "done"}))]))
    run = _last_run(k.bid)
    assert run.status == "stopped"
    assert "rejected" in run.error and "suspended" not in run.error
