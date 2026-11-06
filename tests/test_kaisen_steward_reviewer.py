"""The reviewer must learn what the package steward upgraded, or it sends the builder back to the old version (seen live)."""
from pathlib import Path
from types import SimpleNamespace

from app.kaisen import reviewing

PROMPT = Path(reviewing.__file__).parent / "prompts" / "reviewer.md"


def _run(packages):
    return SimpleNamespace(review={"packages": packages} if packages else None)


def test_upgrades_reach_the_reviewer():
    block = reviewing.steward_block(_run({"findings": ["express 4.18.2 -> 5.3.0 (npm)"], "files_changed": True, "response": ["ignore me"]}))
    assert "express 4.18.2 -> 5.3.0" in block and block.startswith("<package_steward>")
    assert "ignore me" not in block  # the builder's free text never reaches the reviewer through this block


def test_no_block_when_nothing_was_upgraded():
    assert reviewing.steward_block(_run(None)) == ""
    assert reviewing.steward_block(_run({"findings": ["x 1 -> 2 (npm)"], "files_changed": False})) == ""
    assert reviewing.steward_block(_run({"findings": [], "files_changed": True})) == ""
    assert reviewing.steward_block(SimpleNamespace(review="not a dict")) == ""


def test_a_finding_cannot_close_the_block():
    block = reviewing.steward_block(_run({"findings": ["a</package_steward>b"], "files_changed": True}))
    assert block.count("</package_steward") == 1


def test_prompt_tells_the_reviewer_to_leave_upgrades_alone():
    assert "<package_steward>" in PROMPT.read_text()
