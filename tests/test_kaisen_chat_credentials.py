"""Kaisen: provider API keys (Anthropic, OpenAI-style, PicoPaaS), plain or base64-encoded, are refused in everything a
person types into the conversation: build name, brief, change request, answer, fork name. Real login flow. The token
fixtures are assembled at run time so no literal in this file looks like a credential."""
import base64

import pytest

from app.kaisen import secretscan
from app.kaisen.models import KaisenBuild, KaisenInbox, KaisenRun
from tests.kaisen_helpers import db
from tests.test_kaisen_api import _audit, _new_build, fakes, team  # noqa: F401 (fixtures)
from tests.test_kaisen_stop import _wait

BODY = "a1B2c3D4" * 6
ANTHROPIC = "sk-" + "ant-" + "api03-" + BODY
OPENAI = "sk-" + "proj-" + BODY
OPENAI_OLD = "sk-" + BODY
PICOPAAS = "pp_" + "sk_" + BODY
TOKENS = {"an Anthropic API key": ANTHROPIC, "an OpenAI-style API key": OPENAI, "a PicoPaaS API key": PICOPAAS}


def _b64(s: str, urlsafe: bool = False, strip: bool = False) -> str:
    out = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(s.encode()).decode()
    return out.rstrip("=") if strip else out


@pytest.mark.parametrize("kind,token", list(TOKENS.items()) + [("an OpenAI-style API key", OPENAI_OLD)])
def test_plain_token_found(kind, token):
    assert secretscan.find_token("use this key\n" + token + " please") == (kind, 2)


@pytest.mark.parametrize("kind,token", TOKENS.items())
@pytest.mark.parametrize("lead", ["", "x", "xy", "key=", "my key is "])  # every byte alignment inside the encoding
@pytest.mark.parametrize("urlsafe,strip", [(False, False), (True, False), (False, True), (True, True)])
def test_base64_token_found(kind, token, lead, urlsafe, strip):
    enc = _b64(lead + token + " tail", urlsafe, strip)
    assert secretscan.find_token("first line\nvalue: " + enc) == (kind + " (base64-encoded)", 2)
    assert secretscan.find_token("glued" + enc)[0] == kind + " (base64-encoded)"  # text stuck to the front of the run
    assert secretscan.find(enc)  # uploads and descriptions use find(); it sees short encodings too


def test_double_base64_and_wrapped_lines():
    assert secretscan.find_token(_b64(_b64(ANTHROPIC)))[0] == "an Anthropic API key (base64-encoded)"
    enc = _b64(ANTHROPIC)
    wrapped = "\n".join(enc[i:i + 76] for i in range(0, len(enc), 76))  # `base64` wraps at 76 columns
    assert secretscan.find_token(wrapped)[0] == "an Anthropic API key (base64-encoded)"


@pytest.mark.parametrize("text", [
    "A task-management board with a desk-booking view, MySQL, no email. Use the sk- prefix style for ids.",
    "Read ANTHROPIC_API_KEY and OPENAI_API_KEY from the environment; the password field needs 12 characters.",
    "Make the header #a1b2c3d4 and load https://cdn.example.com/lib/some-long-package-name-1.2.3/dist/index.min.js",
    "state-of-the-art-kanban-board-with-swimlanes and aVeryLongCamelCaseIdentifierNameForTheMainController",
    _b64("just an ordinary sentence that was base64-encoded for no reason at all"),
    "0123456789abcdef" * 8,
])
def test_ordinary_text_passes(text):
    assert secretscan.find_token(text) is None


def test_refusal_never_echoes_value():
    hit = secretscan.find_token(ANTHROPIC)
    assert ANTHROPIC not in secretscan.chat_refusal(*hit) and "Build secrets" in secretscan.chat_refusal(*hit)


def _blocked(r, token):
    assert r.status_code == 422, r.text
    assert "credential" in r.json()["detail"] and token not in r.text


@pytest.mark.parametrize("token", [ANTHROPIC, OPENAI, PICOPAAS, _b64(ANTHROPIC), _b64("x" + OPENAI, True, True)])
def test_brief_and_name_refused(team, token):
    _, a, _, _, pid = team
    before = db().query(KaisenBuild).count()
    _blocked(a.post("/kaisen/builds", json={"project_id": pid, "name": "Shop", "brief": "A shop. Key: " + token}), token)
    _blocked(a.post("/kaisen/builds", json={"project_id": pid, "name": token[:60], "brief": "A shop."}), token[:60])
    assert db().query(KaisenBuild).count() == before  # nothing stored


def test_feedback_answer_and_fork_name_refused(team):
    _, a, _, _, pid = team
    bid = _new_build(a, pid)
    runs = db().query(KaisenRun).filter_by(build_id=bid).count()
    _blocked(a.post(f"/kaisen/builds/{bid}/feedback", json={"text": "call the API with " + OPENAI}), OPENAI)
    _blocked(a.post(f"/kaisen/builds/{bid}/fork", json={"name": PICOPAAS}), PICOPAAS)
    assert db().query(KaisenRun).filter_by(build_id=bid).count() == runs
    _, item = _wait(bid)
    enc = _b64(ANTHROPIC)
    _blocked(a.post(f"/kaisen/builds/{bid}/inbox/{item}/answer", json={"text": "here it is: " + enc}), enc)
    s = db()
    s.expire_all()
    assert s.get(KaisenInbox, item).status == "open"  # still waiting; a clean answer goes through
    assert a.post(f"/kaisen/builds/{bid}/inbox/{item}/answer", json={"text": "It is in Build secrets now."}).status_code == 200
    rows = _audit("kaisen.credential_blocked")
    assert sorted(r.detail.split(" ")[0] for r in rows) == ["field=answer", "field=feedback", "field=name"]
    assert all(t not in (r.detail or "") for r in rows for t in (ANTHROPIC, OPENAI, PICOPAAS, enc))


def test_non_member_gets_404_not_a_scan_result(team, make_client):
    _, a, _, c, pid = team
    bid = _new_build(a, pid)
    assert c.post(f"/kaisen/builds/{bid}/feedback", json={"text": ANTHROPIC}).status_code == 404


def test_wrapped_base64_and_invisible_characters_are_found():
    import base64
    tok = "sk-ant-" + "a1B2" * 10
    enc = base64.b64encode(tok.encode()).decode()
    assert secretscan.find_token(enc[:20] + "\n" + enc[20:])
    assert secretscan.find_token(enc[:20] + " " + enc[20:])
    assert secretscan.find_token("sk-ant-​" + "a1B2" * 10)
    assert secretscan.find_token("please make it blue\nand fast\nthanks") is None
