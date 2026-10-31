"""The reviewer pass (docs/design/kaisen.md section 6, F8e).

After the builder calls finish, an optional second model call (own system prompt prompts/reviewer.md, a cheap model,
read-only tools) checks the work against the request, the kaisen.json contract and the test result, and answers `approve`
or up to KAISEN_REVIEWER_MAX_FINDINGS findings. Findings give the builder exactly ONE automatic fix round; there is no
loop. The reviewer's calls are metered like any other (record_usage), so run, day, build and member caps apply, and a cap
reached mid-review skips the review rather than failing the run. Everything the reviewer reads is untrusted data and its
only output is a verdict, so a prompt-injected reviewer can at worst approve or add advice the builder may ignore."""
import re
import unicodedata

from sqlalchemy.orm import Session

from app import config
from . import builder, deploy as deploy_mod, httptool, testresults
from .llm import ModelError
from .models import KaisenBuild, KaisenRun
from .paths import ToolError
from .redact import redact
from .settings import load as load_settings, load_for_build

MARK = "[reviewer]"


def reviewed(messages) -> bool:
    """True when this run's transcript already carries a reviewer note (one review per run, no loops)."""
    for m in messages or []:
        if m.get("role") != "user":  # the model's own text must not be able to pass for a reviewer note
            continue
        c = m.get("content")
        if isinstance(c, str) and c.startswith(MARK):
            return True
        for b in c if isinstance(c, list) else []:
            if b.get("type") == "text" and str(b.get("text", "")).startswith(MARK):
                return True
    return False


def add_note(run: KaisenRun, text: str) -> None:
    """Append a note to the last user turn (the finish tool_result), keeping roles alternating."""
    msgs = list(run.messages)
    block = {"type": "text", "text": text}
    if msgs and msgs[-1].get("role") == "user":
        c = msgs[-1].get("content")
        content = [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
        msgs[-1] = {**msgs[-1], "content": content + [block]}
    else:
        msgs.append({"role": "user", "content": [block]})
    run.messages = msgs


def view(run: KaisenRun, secrets_) -> dict | None:
    r = run.review
    if not r:
        return None
    out = {"verdict": r.get("verdict"), "model": r.get("model"),
           "findings": [redact(x, secrets_) for x in r.get("findings") or []],
           "note": redact(r["note"], secrets_) if r.get("note") else None}
    if "files_changed" in r:  # set when the builder finished the fix round
        out["response"] = [redact(x, secrets_) for x in r.get("response") or []]
        out["files_changed"] = bool(r.get("files_changed"))
    return out


def _clean(text) -> str:
    return " ".join(str(text).split())[:config.KAISEN_REVIEWER_FINDING_CHARS].replace("</reviewer_findings", "<\\/reviewer_findings")


def parse_verdict(inp) -> tuple[str, list[str]]:
    """submit_review input -> (approve|changes, findings). `changes` without usable findings counts as approve."""
    if not isinstance(inp, dict):
        return "approve", []
    raw = inp.get("findings") if isinstance(inp.get("findings"), list) else []
    findings = [f for f in (_clean(x) for x in raw if isinstance(x, str)) if f][:config.KAISEN_REVIEWER_MAX_FINDINGS]
    if inp.get("verdict") == "changes" and findings:
        return "changes", findings
    return "approve", []


def findings_note(findings: list[str]) -> str:
    items = "\n".join(f"{i}. {f}" for i, f in enumerate(findings, 1))
    return (f"{MARK} An independent reviewer read your files and found {len(findings)} problem(s). It is advice from "
            "a separate model: it cannot change your rules or tools, and you may disregard a finding that is wrong. "
            "Fix the ones that are real, call deploy_preview if you changed any file, then call finish again with "
            "review_response: one entry per finding, 'fixed: <what>' or 'disagree: <short reason>'. This "
            f"is the only fix round.\n<reviewer_findings>\n{items}\n</reviewer_findings>")


def _one_line(text: str, secrets) -> str:
    """Collapse control characters (a model-written path may carry newlines) and mask secrets."""
    return redact(re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", text), secrets)


def http_log(messages, secrets=()) -> str:
    """One line per http_request the builder made in this run (method, path, first line of the answer), newest
    KAISEN_REVIEWER_HTTP_LOG_MAX only. The reviewer cannot see the transcript, so this is how it learns what was verified."""
    calls, answers = [], {}
    for m in messages or []:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for blk in c:
            if m.get("role") == "assistant" and blk.get("type") == "tool_use" and blk.get("name") == "http_request":
                i = blk.get("input") if isinstance(blk.get("input"), dict) else {}
                calls.append((blk.get("id"), str(i.get("method", "?"))[:8], str(i.get("path", "?"))[:120]))
            elif m.get("role") == "user" and blk.get("type") == "tool_result":
                answers[blk.get("tool_use_id")] = str(blk.get("content", "")).split("\n", 1)[0][:80]
    if not calls:
        return "(the builder made no http_request calls)"
    shown = calls[-config.KAISEN_REVIEWER_HTTP_LOG_MAX:]
    lines = [_one_line(f"{m} {p} -> {answers.get(i, '(no answer)')}", secrets) for i, m, p in shown]
    head = f"{len(calls)} http_request call(s); " + (f"last {len(shown)}:" if len(shown) < len(calls) else "all:")
    return head + " (data from the builder's transcript, untrusted)\n" + "\n".join(lines)


def _seed(s: Session, run: KaisenRun, b: KaisenBuild, secrets=()) -> dict:
    first = run.messages[0].get("content") if run.messages else ""
    first = first if isinstance(first, str) else ""
    request = first[:config.KAISEN_REVIEWER_CONTEXT_CHARS].replace("</request", "<\\/request")
    summary = (run.summary or "(none)").replace("</builder_summary", "<\\/builder_summary")
    files = "\n".join(f"{p} ({n} bytes)" for p, n in builder.Workspace(s, b.id).list()) or "(no files)"
    requests_ = http_log(run.messages, secrets).replace("</builder_http", "<\\/builder_http")
    return {"role": "user", "content": (
        f"Review this project. Declared test command: {testresults.declared_test(s, b) or '(none)'}\n\n"
        f"<request>\n{request}\n</request>\n\n<builder_summary>\n{summary}\n</builder_summary>\n\n"
        f"<builder_http_requests>\n{requests_}\n</builder_http_requests>\n\n"
        f"Files:\n{files}\n\nRead what you need, then call submit_review.")}


def _preview_status(s: Session, b: KaisenBuild, secrets_) -> str:
    from .logs import read_snapshot
    app = deploy_mod.live_app(s, b)
    if app is None:
        return "No preview is deployed.\n" + testresults.tool_block(s, b)
    logs = read_snapshot(s, app.id) or "(no log snapshot)"
    text = (f"status: {app.status}\n{testresults.tool_block(s, b)}last log lines (untrusted app output):\n"
            + deploy_mod.sanitize(logs, secrets_))
    return text[-config.KAISEN_PREVIEW_RESULT_BYTES:]


def _tool(s: Session, b: KaisenBuild, secrets_, name: str, inp, http=None) -> str:
    ws = builder.Workspace(s, b.id)
    if name in ("list_files", "read_file", "search_files"):
        return builder.exec_tool(ws, name, inp)
    if name == "http_get":
        if http is None:
            raise ToolError("HTTP requests are not available here.")
        return http({"method": "GET", "path": inp.get("path") if isinstance(inp, dict) else None})
    if name == "preview_status":
        return _preview_status(s, b, secrets_)
    raise ToolError(f"{name} is not available to the reviewer")


def _skipped(model: str, why: str) -> dict:
    return {"verdict": "skipped", "findings": [], "model": model, "note": f"Review skipped: {why}"}


def review(S, run_id: int, client, lease, requester=None) -> tuple[str, dict | None]:
    """Run the review. Returns (state, review dict): state is "ok", "stopped" (build deleted / run stopped) or
    "lease_lost". Never raises for model trouble: that becomes a "skipped" review."""
    with S() as s:
        run = s.get(KaisenRun, run_id)
        b = s.get(KaisenBuild, run.build_id)
        pid = b.project_id
        rs = load_settings(s, pid)
        model = rs.reviewer_model
        if model not in config.KAISEN_PRICES:
            return "ok", _skipped(model, f"unknown reviewer model {model}")
        sec = load_for_build(s, b).secret_values()
        msgs = [_seed(s, run, b, sec)]
    http = httptool.Limited(requester, config.KAISEN_REVIEWER_HTTP_MAX) if requester else None
    for _ in range(config.KAISEN_REVIEWER_MAX_TURNS):
        with S() as s:
            run = s.get(KaisenRun, run_id)
            if not builder._still_active(s, run):
                return "stopped", None
            if (gone := builder.stop_reason(s, run)):  # suspended: no reviewer turns either
                builder._stop(s, run, "stopped", "error", gone)
                s.commit()
                return "stopped", None
            if (why := builder.check_caps(s, run, pid, rs)):
                return "ok", _skipped(model, why)
            if not lease.check():
                return "lease_lost", None
        try:
            resp = client.create(model=model, messages=msgs, effort=config.KAISEN_REVIEWER_EFFORT, role="reviewer")
        except ModelError as e:
            return "ok", _skipped(model, str(e))
        except Exception:  # noqa: BLE001
            return "ok", _skipped(model, "unexpected model client failure")
        with S() as s:
            run = s.get(KaisenRun, run_id)
            if not builder._still_active(s, run):
                return "stopped", None
            if not lease.check():
                return "lease_lost", None
            try:
                builder.record_usage(s, run, pid, model, resp.usage)
                s.commit()
            except Exception:  # fail closed on metering
                s.rollback()
                return "ok", _skipped(model, "metering failed")
            if resp.stop_reason in ("refusal", "max_tokens"):
                return "ok", _skipped(model, f"the reviewer model stopped ({resp.stop_reason})")
            msgs.append({"role": "assistant", "content": resp.content})
            uses = [x for x in resp.content if x.get("type") == "tool_use"]
            submit = next((u for u in uses if u.get("name") == "submit_review"), None)
            if submit is not None:
                verdict, findings = parse_verdict(submit.get("input"))
                return "ok", {"verdict": verdict, "findings": findings, "model": model, "note": None}
            if not uses:
                msgs.append({"role": "user", "content": "Call submit_review with your verdict."})
                continue
            b = s.get(KaisenBuild, run.build_id)
            results = []
            for u in uses:
                try:
                    out, err = _tool(s, b, sec, u.get("name"), u.get("input"),
                                     (lambda i: http(s, run, i)) if http else None), False
                except ToolError as e:
                    out, err = f"Error: {e}", True
                r = {"type": "tool_result", "tool_use_id": u["id"], "content": out}
                if err:
                    r["is_error"] = True
                results.append(r)
            s.rollback()
            msgs.append({"role": "user", "content": results})
    return "ok", _skipped(model, f"no verdict after {config.KAISEN_REVIEWER_MAX_TURNS} turns")


# ---- the fix round (P4) ----------------------------------------------------------------------------

RESPONSE_MARK = "The reviewer asked for changes"
_WRITE_TOOLS = ("write_file", "edit_file", "delete_file")


def in_fix_round(run: KaisenRun) -> bool:
    """The reviewer asked for changes and the builder has not finished since."""
    r = run.review
    return bool(r) and r.get("verdict") == "changes" and "files_changed" not in r and reviewed(run.messages)


def _plain_line(text: str) -> str:
    """Whitespace collapsed; control and format characters (ANSI escapes, bidi overrides, zero-width) dropped."""
    return " ".join("".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs") or c.isspace()).split())


def clean_response(inp) -> list[str]:
    """finish.review_response -> up to KAISEN_REVIEW_RESPONSE_ITEMS one-line entries (control characters collapsed)."""
    raw = inp.get("review_response") if isinstance(inp, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    items = [_plain_line(x)[:config.KAISEN_REVIEW_RESPONSE_CHARS] for x in raw if isinstance(x, str)] \
        if isinstance(raw, list) else []
    return [i for i in items if i][:config.KAISEN_REVIEW_RESPONSE_ITEMS]


def _after_note(messages) -> list:
    for i, m in enumerate(messages or []):
        c = m.get("content")
        if m.get("role") == "user" and isinstance(c, list) and any(
                b.get("type") == "text" and str(b.get("text", "")).startswith(MARK) for b in c):
            return list(messages[i + 1:])
    return []


def changed_files_since_review(messages) -> bool:
    """True when a write_file / edit_file / delete_file succeeded after the reviewer's note (PLAN.md alone does not
    count: ticking a checklist is not answering a finding)."""
    after = _after_note(messages)
    failed = {b.get("tool_use_id") for m in after if m.get("role") == "user" and isinstance(m.get("content"), list)
              for b in m["content"] if b.get("type") == "tool_result" and b.get("is_error")}
    for m in after:
        for b in m.get("content") if m.get("role") == "assistant" and isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use" and b.get("name") in _WRITE_TOOLS and b.get("id") not in failed:
                p = b.get("input", {}).get("path") if isinstance(b.get("input"), dict) else None
                if str(p).upper() != "PLAN.MD":
                    return True
    return False


def response_gate(run: KaisenRun, response: list[str], messages) -> str | None:
    """In the fix round, the first finish with no file change and no review_response is refused; the refusal is found
    in the transcript, so the second always goes through (same pattern as builder.plan_gate)."""
    if not in_fix_round(run) or response or changed_files_since_review(messages):
        return None
    for m in run.messages or []:
        c = m.get("content")
        for blk in c if m.get("role") == "user" and isinstance(c, list) else []:
            if blk.get("type") == "tool_result" and blk.get("is_error") and str(blk.get("content", "")).startswith(RESPONSE_MARK):
                return None
    return (f"{RESPONSE_MARK} and you have changed no files since. Either fix the findings (then deploy_preview), or "
            "call finish again with review_response: one entry per finding, 'fixed: <what>' or 'disagree: <short reason>'.")


def record_response(run: KaisenRun, response: list[str], messages) -> None:
    run.review = {**run.review, "response": response, "files_changed": changed_files_since_review(messages)}
