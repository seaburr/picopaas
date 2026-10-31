#!/usr/bin/env python3
"""Manual Kaisen eval (design s15 H6). NEVER run in CI: it spends real money on the project's Anthropic key.

Drives the public PicoPaaS API with an API token: for each brief it creates a build, starts it, polls until the build
settles, runs HTTP checks against the preview and prints a markdown table (design s14 style) plus totals.

    E2E_USER_TOKEN=... python scripts/kaisen_eval.py --project-id 123 --briefs "Habit tracker" --max-usd 0.50

The token is read from an environment variable (--token-env) and is never printed or written: data files hold only
API response bodies, never request headers, and any occurrence of the token is scrubbed before writing.

Data collection (--data-dir): per brief <slug>/detail.json, code.tar.gz, run-<id>-diff.json, checks.json, poll.jsonl,
plus summary.json and summary.md at the top. --model/--effort set the project's Kaisen model for the run and restore
the previous values afterwards.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import secrets
import tarfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "https://platform.picopaas.xyz"
BRIEFS_FILE = Path(__file__).with_name("kaisen_eval_briefs.json")
API_RETRIES = 4  # attempts for a platform GET/DELETE that hits a network error
API_RETRY_SLEEP = 5.0  # seconds, times the attempt number
POLL_SECONDS = 10
TIMEOUT_SECONDS = 20 * 60
ASSUMPTION_ANSWER = "Decide yourself and note the assumption."
NO_RESUME_WORDS = ("cap", "refus")  # a spend cap or refusal will not fix itself on resume
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
SETTLE_SECONDS = 300
TEST_FINAL = ("pass", "fail", "timeout")


class EvalError(Exception):
    pass


def load_briefs(path: Path = BRIEFS_FILE, names: list[str] | None = None) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise EvalError(f"cannot read briefs file {path}: {e}") from e
    if not isinstance(data, list) or not data:
        raise EvalError("briefs file must hold a non-empty JSON list")
    seen = set()
    for i, b in enumerate(data):
        if not isinstance(b, dict):
            raise EvalError(f"brief #{i} is not an object")
        name, text = b.get("name"), b.get("brief")
        if not isinstance(name, str) or not name or len(name) > 60:
            raise EvalError(f"brief #{i}: name must be 1-60 characters")
        if name in seen:
            raise EvalError(f"duplicate brief name {name!r}")
        seen.add(name)
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise EvalError(f"brief {name!r}: brief text must be 1-8000 characters")
        if "version" in b and not isinstance(b["version"], int):
            raise EvalError(f"brief {name!r}: version must be an integer")
        checks = b.get("checks")
        if not isinstance(checks, list) or not checks:
            raise EvalError(f"brief {name!r}: checks must be a non-empty list")
        flow = b.get("flow", [])
        if not isinstance(flow, list):
            raise EvalError(f"brief {name!r}: flow must be a list of steps")
        for c in checks + flow:
            if not isinstance(c, dict) or c.get("method") not in METHODS or not str(c.get("path", "")).startswith("/"):
                raise EvalError(f"brief {name!r}: each check needs a method and a path starting with /")
            exp = c.get("expect_status")
            if not (isinstance(exp, int) or (isinstance(exp, list) and exp and all(isinstance(x, int) for x in exp))):
                raise EvalError(f"brief {name!r}: check {c['path']} needs expect_status (int or list of ints)")
    if names:
        by = {}
        for b in data:
            by[slugify(b["name"])] = b
            by[b["name"].lower()] = b
        names = [n if n.lower() in by else slugify(n) for n in names]
        missing = [n for n in names if n.lower() not in by]
        if missing:
            raise EvalError(f"unknown brief(s): {', '.join(missing)}; known: {', '.join(b['name'] for b in data)}")
        return [by[n.lower()] for n in names]
    return data


TOOLS = ("write_file", "edit_file", "read_file", "search_files", "deploy_preview", "http_request", "ask_human", "finish")
SNIPPET = 2048
_CALL = re.compile(r"^\[call\] (\S+)")
_HTTP = re.compile(r"^\[result\] HTTP (\d{3})")
_TICK = re.compile(r"^\s*[-*]\s+\[([ xX])\]", re.M)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "brief"


def parse_ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except (ValueError, AttributeError):
        return None


def run_seconds(run: dict) -> float | None:
    a, b = parse_ts(run.get("started_at")), parse_ts(run.get("finished_at"))
    if a is None or b is None:
        return None
    try:
        return round((b - a).total_seconds(), 1)
    except TypeError:  # naive vs aware
        return None


def deploy_ok_seen(runs: list[dict]) -> bool:
    for r in runs or []:
        for line in r.get("transcript") or []:
            if isinstance(line, dict) and "Result: OK" in str(line.get("text", "")):
                return True
    return False


def analyze_transcripts(runs: list[dict]) -> dict:
    """Behaviour counters from runs[].transcript (list of {role, text}). Calls render as '[call] name {json}', results
    as '[result] ...'; results carry no tool name, so they are paired with calls in order. Parsing is tolerant."""
    tools = {t: 0 for t in TOOLS}
    other = 0
    m = {"tool_calls": tools, "other_tool_calls": 0, "write_warnings": 0, "deploy_not_ok": 0, "deploy_ok": 0,
         "http_status": {}, "repeat_stops": 0, "max_tokens_stops": 0, "turn_limit_stops": 0,
         "context_resets": 0, "transcripts_missing": 0,
         # feature verification: distinct http_request paths per method, requests that carried a session
         # (Cookie/Authorization header, or any request after a response that set a cookie), and PLAN finish-gate hits
         "http_paths": {}, "http_authed": 0, "set_cookie_seen": 0, "plan_gate": 0,
         "web_searches": 0, "web_fetches": 0}
    paths: dict[str, set] = {}
    for r in sorted(runs or [], key=lambda x: x.get("id") or 0):
        m["context_resets"] += int(r.get("context_resets") or 0)
        lines = r.get("transcript")
        err = str(r.get("error") or "")
        if not isinstance(lines, list):
            m["transcripts_missing"] += 1
            lines = []
        pending: list[tuple[str, dict]] = []
        cookie_set = False
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            text = str(ln.get("text") or "")
            m["plan_gate"] += text.count("PLAN.md still has")
            if text.startswith("[web] searched for"):
                m["web_searches"] += 1
            elif text.startswith("[web] read"):
                m["web_fetches"] += 1
            c = _CALL.match(text)
            if c:
                name = c.group(1)
                inp = _call_input(text, name)
                pending.append((name, inp))
                if name == "http_request":
                    meth = str(inp.get("method") or "GET").upper()
                    path = urlsplit(str(inp.get("path") or inp.get("url") or "")).path or "/"
                    paths.setdefault(meth, set()).add(path)
                    hdrs = inp.get("headers") if isinstance(inp.get("headers"), dict) else {}
                    if cookie_set or any(str(k).lower() in ("cookie", "authorization") for k in hdrs):
                        m["http_authed"] += 1
                if name in tools:
                    tools[name] += 1
                else:
                    other += 1
                continue
            if not text.startswith("[result]"):
                continue
            name = pending.pop(0)[0] if pending else ""
            if name == "http_request" and "set-cookie" in text.lower():
                m["set_cookie_seen"] += 1
                cookie_set = True
            if name in ("write_file", "edit_file"):
                m["write_warnings"] += text.count("WARNING:")
            if "Result: NOT OK" in text:
                m["deploy_not_ok"] += 1
            elif "Result: OK" in text:
                m["deploy_ok"] += 1
            h = _HTTP.match(text)
            if h:
                m["http_status"][h.group(1)] = m["http_status"].get(h.group(1), 0) + 1
            low = text.lower()
            if "repeat detector" in low:
                m["repeat_stops"] += 1
                err = ""  # same event also stored as run.error; count once
            if "max_tokens" in low:
                m["max_tokens_stops"] += 1
                err = err if "max_tokens" not in err else ""
        low = err.lower()
        m["repeat_stops"] += "repeat detector" in low
        m["max_tokens_stops"] += "max_tokens" in low
        m["turn_limit_stops"] += "turn limit" in low
    m["other_tool_calls"] = other
    m["http_paths"] = {k: len(v) for k, v in sorted(paths.items())}
    return m


def _call_input(text: str, name: str) -> dict:
    try:
        v = json.loads(text[len(f"[call] {name}"):].strip())
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def analyze_tarball(data: bytes) -> dict:
    """Facts about the saved code. The generated CONVERSATION file is not counted."""
    out = {"files": 0, "bytes": 0, "has_tests": False, "has_readme": False, "dockerfile_user": None,
           "kaisen_json": None, "plan_ticked": None, "plan_unticked": None, "error": None}
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as e:
        out["error"] = f"bad tarball: {type(e).__name__}"
        return out
    with tf:
        for mem in tf.getmembers():
            if not mem.isfile():
                continue
            rel = mem.name.split("/", 1)[1] if "/" in mem.name else mem.name
            base = rel.rsplit("/", 1)[-1]
            if base.upper().startswith("CONVERSATION"):
                continue
            out["files"] += 1
            out["bytes"] += mem.size
            low = rel.lower()
            if "test" in low.split("/")[-1] or low.startswith(("tests/", "test/")) or "/tests/" in low:
                out["has_tests"] = True
            if rel.lower() in ("readme.md", "readme", "readme.txt"):
                out["has_readme"] = True
            f = tf.extractfile(mem)
            txt = f.read(200_000).decode("utf-8", "replace") if f and mem.size <= 200_000 else ""
            if rel == "Dockerfile":
                users = [ln.strip() for ln in txt.splitlines() if ln.strip().upper().startswith("USER ")]
                out["dockerfile_user"] = users[-1] if users else ""
            elif rel == "kaisen.json":
                try:
                    out["kaisen_json"] = json.loads(txt)
                except ValueError:
                    out["kaisen_json"] = {"unparseable": txt[:200]}
            elif rel == "PLAN.md":
                ticks = _TICK.findall(txt)
                out["plan_ticked"] = sum(t != " " for t in ticks)
                out["plan_unticked"] = sum(t == " " for t in ticks)
    return out


def evaluate_check_detail(client: httpx.Client, base: str, check: dict) -> dict:
    """Run one HTTP check against the preview. Never records request headers."""
    kwargs = {}
    if "body" in check:
        kwargs["json"] = check["body"]
    if "csv" in check:  # raw text/csv body
        kwargs["content"] = check["csv"].encode()
        kwargs["headers"] = {"Content-Type": "text/csv"}
    resp = None
    err = ""
    attempts = 0
    for attempt in range(3 if check.get("retry", True) else 1):  # a fresh preview can need a moment
        attempts += 1
        try:
            resp = client.request(check["method"], base + check["path"], **kwargs)
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}"
            resp = None
        if resp is not None and resp.status_code < 500:
            break
        if attempt < 2 and check.get("retry", True):
            time.sleep(CHECK_RETRY_SLEEP)
    rec = {"request": {"method": check["method"], "path": check["path"], "body": check.get("body", check.get("csv"))},
           "expect_status": check["expect_status"], "expect_contains": check.get("expect_contains"),
           "status": resp.status_code if resp is not None else None,
           "body_snippet": resp.text[:SNIPPET] if resp is not None else "", "attempts": attempts}
    tag = f"{check['method']} {check['path']}"
    for var, key in (check.get("capture") or {}).items():  # flow: remember a JSON field of the answer
        try:
            rec.setdefault("captured", {})[var] = resp.json()[key]
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
    if resp is None:
        rec.update(passed=False, reason=f"{tag}: request failed ({err})")
        return rec
    exp = check["expect_status"]
    exp = exp if isinstance(exp, list) else [exp]
    want = check.get("expect_contains")
    if resp.status_code not in exp:
        rec.update(passed=False, reason=f"{tag}: status {resp.status_code}, wanted {exp}")
    elif want and want not in resp.text:
        rec.update(passed=False, reason=f"{tag}: body lacks {want!r}")
    else:
        rec.update(passed=True, reason="ok")
    return rec


def evaluate_check(client: httpx.Client, base: str, check: dict) -> tuple[bool, str]:
    rec = evaluate_check_detail(client, base, check)
    return rec["passed"], rec["reason"]


CHECK_RETRY_SLEEP = 5


def preview_base(detail: dict) -> str | None:
    host = detail.get("app_hostname") or (detail.get("app") or {}).get("hostname")
    if not host:
        return None
    return host.rstrip("/") if "://" in host else f"https://{host}"


def subst(obj, vars: dict):
    """Replace {name} in strings (recursively). A string that is exactly one {name} takes the variable's own type."""
    if isinstance(obj, str):
        for k, v in vars.items():
            if obj == "{" + k + "}":
                return v
            obj = obj.replace("{" + k + "}", str(v))
        return obj
    if isinstance(obj, list):
        return [subst(x, vars) for x in obj]
    if isinstance(obj, dict):
        return {k: subst(v, vars) for k, v in obj.items()}
    return obj


def new_row(brief: dict, model: str = "") -> dict:
    return {"name": brief["name"], "slug": slugify(brief["name"]), "model": model, "status": "error", "runs": 0,
            "steps": 0, "cost": 0.0, "minutes": 0.0, "tests": "-", "checks_passed": 0,
            "checks_total": len(brief["checks"]) + len(brief.get("flow") or []), "review": "-", "notes": [], "metrics": {}, "code": {},
            "first_deploy_ok_min": None, "run_seconds": [], "settle": {}}


class Evaluator:
    def __init__(self, api: httpx.Client, check_client: httpx.Client, *, project_id: int, max_usd: float,
                 keep: bool = False, sleep=time.sleep, clock=time.monotonic,
                 poll_seconds: float = POLL_SECONDS, timeout_seconds: float = TIMEOUT_SECONDS, log=None,
                 data_dir: Path | str | None = None, model: str | None = None, effort: str | None = None,
                 scrub: list[str] | None = None, settle_seconds: float = SETTLE_SECONDS,
                 web: bool | None = None, web_domains: list[str] | None = None):
        self.api, self.check_client = api, check_client
        self.project_id, self.max_usd, self.keep = project_id, max_usd, keep
        self.sleep, self.clock = sleep, clock
        self.poll_seconds, self.timeout_seconds = poll_seconds, timeout_seconds
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self.data_dir = Path(data_dir) if data_dir else None
        self.want_model, self.want_effort = model, effort
        self.want_web, self.want_web_domains = web, web_domains
        self.scrub = [x for x in (scrub or []) if x]
        self.settle_seconds = settle_seconds
        self._tar: bytes | None = None
        self._tar_err = ""
        self._tar_tried = False
        self.model_used = model or ""
        self.previous: dict | None = None
        self._polls: list[dict] = []
        self._first_ok_at: float | None = None

    # -- data files (only API response bodies are ever written, never request headers)
    def _clean(self, text: str) -> str:
        for sec in self.scrub:
            text = text.replace(sec, "[redacted]")
        return text

    def _write(self, rel: str, content: str | bytes) -> None:
        if self.data_dir is None:
            return
        path = self.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(self._clean(content))

    def _write_json(self, rel: str, obj) -> None:
        self._write(rel, json.dumps(obj, indent=2, default=str))

    # -- project settings
    def apply_settings(self) -> None:
        """Set the project's model/effort for the eval, remembering the previous values for restore_settings."""
        try:
            cur = self._req("GET", f"/projects/{self.project_id}/kaisen").json()
        except (EvalError, httpx.HTTPError):
            if self.want_model or self.want_effort or self.want_web is not None or self.want_web_domains is not None:
                raise
            return  # only reading for the record; not fatal
        self.model_used = self.want_model or cur.get("model", "")
        wants = {}
        if self.want_web is not None:
            wants["web_access"] = self.want_web
        if self.want_web_domains is not None:
            wants["web_allowed_domains"] = self.want_web_domains
        if not (self.want_model or self.want_effort or wants):
            return
        body = {}
        self.previous = {}
        for k, v in wants.items():
            self.previous[k] = cur.get(k)
            body[k] = v
        if self.want_model:
            self.previous["model"] = cur.get("model")
            body["model"] = self.want_model
        if self.want_effort:
            self.previous["effort"] = cur.get("effort")
            body["effort"] = self.want_effort
        self._req("PUT", f"/projects/{self.project_id}/kaisen", json=body)
        self.log(f"project {self.project_id}: settings set to {body} (was {self.previous})")

    def restore_settings(self) -> bool:
        if not self.previous:
            return True
        try:
            self._req("PUT", f"/projects/{self.project_id}/kaisen", json=self.previous)
        except (EvalError, httpx.HTTPError) as e:
            self.log(f"WARNING: could not restore project settings {self.previous}: {e}")
            return False
        self.log(f"project {self.project_id}: settings restored to {self.previous}")
        self.previous = None
        return True

    # -- API helpers
    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        # Reads (and DELETE, which is idempotent) retry a transport blip; a POST/PUT is never repeated.
        tries = API_RETRIES if method in ("GET", "HEAD", "DELETE") else 1
        for attempt in range(tries):
            try:
                r = self.api.request(method, path, **kw)
                break
            except httpx.TransportError as e:
                if attempt == tries - 1:
                    raise
                self.log(f"  {method} {path}: {type(e).__name__}, retrying")
                self.sleep(API_RETRY_SLEEP * (attempt + 1))
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", "")
            except ValueError:
                detail = ""
            raise EvalError(f"{method} {path} -> {r.status_code} {str(detail)[:200]}")
        return r

    def _detail(self, build_id: int) -> dict:
        return self._req("GET", f"/kaisen/builds/{build_id}").json()

    # -- poll loop
    def _record_poll(self, d: dict) -> None:
        t = round(self.clock() - self._t0, 1)
        runs = d.get("runs") or []
        latest = runs[0] if runs else {}
        self._polls.append({"t": t, "status": d.get("status"), "run_id": latest.get("id"), "step": latest.get("step"),
                            "last_tool": latest.get("last_tool"), "cost_usd": d.get("cost_usd") if d.get("cost_usd") is not None
                            else latest.get("cost_usd"), "context_resets": latest.get("context_resets")})
        if self._first_ok_at is None and deploy_ok_seen(runs):
            self._first_ok_at = t

    def wait_settled(self, build_id: int, state: dict) -> dict:
        """Poll until done/stopped/error. Answers a waiting question once, resumes a stop/error once."""
        deadline = self.clock() + self.timeout_seconds
        while True:
            d = self._detail(build_id)
            self._record_poll(d)
            st = d.get("status")
            if st == "done":
                return d
            if st == "waiting":
                open_q = [i for i in d.get("inbox", []) if i.get("kind") == "question" and i.get("status") == "open"]
                if open_q and state["answered"]:
                    return d  # asked again: leave it waiting
                if open_q:
                    self._req("POST", f"/kaisen/builds/{build_id}/inbox/{open_q[0]['id']}/answer",
                              json={"text": ASSUMPTION_ANSWER})
                    state["answered"] = True
                    self.log(f"  answered question {open_q[0]['id']}")
                # no open question: the answer was just taken, keep polling
            elif st in ("stopped", "error"):
                reason = (d.get("last_error") or "").lower()
                if state["resumed"] or any(w in reason for w in NO_RESUME_WORDS):
                    return d
                self._req("POST", f"/kaisen/builds/{build_id}/run")
                state["resumed"] = True
                self.log(f"  {st}: resumed once")
            if self.clock() >= deadline:
                d["status"] = "timeout"
                return d
            self.sleep(self.poll_seconds)

    # -- one brief
    def run_brief(self, brief: dict) -> dict:
        row = new_row(brief, self.model_used)
        self._polls, self._first_ok_at = [], None
        self._tar, self._tar_err, self._tar_tried = None, "", False
        start = self._t0 = self.clock()
        build_id = None
        final: dict | None = None
        check_records: list[dict] = []
        try:
            r = self._req("POST", "/kaisen/builds", json={"project_id": self.project_id,
                                                          "name": f"eval {brief['name']}"[:60], "brief": brief["brief"]})
            created = r.json()
            build_id = created["id"]
            self.log(f"{brief['name']}: build {build_id}")
            if created.get("start_error"):
                raise EvalError(f"build not started: {created['start_error']}")
            if created.get("status") == "idle":
                self._req("POST", f"/kaisen/builds/{build_id}/run")
            state = {"answered": False, "resumed": False}
            d = final = self.wait_settled(build_id, state)
            row["minutes"] = round((self.clock() - start) / 60, 1)
            self._fill(row, d)
            if state["answered"]:
                row["notes"].append("answered ask_human")
            if state["resumed"]:
                row["notes"].append("resumed once")
            if d["status"] == "done":
                d = final = self._settle(build_id, d, row)
                self._fill(row, d)
                base = preview_base(d)
                if base:
                    check_records = self._run_checks(row, brief["checks"], base)
                    check_records += self._run_flow(row, brief.get("flow") or [], base)
                else:
                    row["notes"].append("no preview hostname")
        except EvalError as e:
            row["notes"].append(str(e))
        except httpx.HTTPError as e:  # the platform stopped answering mid-brief: record it, clean up, go on
            row["notes"].append(f"platform request failed: {type(e).__name__}")
        finally:
            try:
                self._collect(row, build_id, final, check_records)
            except Exception as e:  # data collection must never block the cleanup below
                row["notes"].append(f"data collection failed: {type(e).__name__}: {e}")
            if build_id is not None and not self.keep:
                try:
                    self._req("DELETE", f"/kaisen/builds/{build_id}", params={"delete_app": "true"})
                except (EvalError, httpx.HTTPError) as e:
                    row["notes"].append(f"cleanup failed: {e}")
        return row

    def _collect(self, row: dict, build_id: int | None, final: dict | None, check_records: list[dict]) -> None:
        """Metrics always; files only with a data dir. Runs before the build is deleted."""
        if build_id is None:
            return
        slug = row["slug"]
        if final is None:  # the poll loop died: fetch whatever exists
            try:
                final = self._detail(build_id)
            except (EvalError, httpx.HTTPError):
                final = None
        runs = (final or {}).get("runs") or []
        row["metrics"] = analyze_transcripts(runs)
        row["run_seconds"] = [run_seconds(r) for r in sorted(runs, key=lambda x: x.get("id") or 0)]
        if self._first_ok_at is not None:
            row["first_deploy_ok_min"] = round(self._first_ok_at / 60, 1)
        tar = self._download(build_id)
        if self._tar_err:
            row["notes"].append(f"no code download: {self._tar_err}")
        if tar:
            row["code"] = analyze_tarball(tar)
        if self.data_dir is None:
            return
        if final is not None:
            self._write_json(f"{slug}/detail.json", final)
        if tar:
            self._write(f"{slug}/code.tar.gz", tar)
        for r in runs:
            if not r.get("has_diff"):
                continue
            try:
                resp = self.api.get(f"/kaisen/builds/{build_id}/runs/{r['id']}/diff")
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                self._write(f"{slug}/run-{r['id']}-diff.json", resp.text)
            # 404 (endpoint not deployed yet or no restore point): tolerated
        self._write_json(f"{slug}/checks.json", check_records)
        self._write(f"{slug}/poll.jsonl", "".join(json.dumps(p) + "\n" for p in self._polls))

    def _download(self, build_id: int) -> bytes | None:
        if not self._tar_tried:
            self._tar_tried = True
            try:
                self._tar = self._req("GET", f"/kaisen/builds/{build_id}/download").content
            except (EvalError, httpx.HTTPError) as e:
                self._tar_err = str(e)[:80]
        return self._tar

    def _declares_test(self, build_id: int, d: dict) -> bool:
        if d.get("test") or (d.get("manifest") or {}).get("test"):
            return True
        tar = self._download(build_id)
        kj = analyze_tarball(tar).get("kaisen_json") if tar else None
        return bool(isinstance(kj, dict) and kj.get("test"))

    def _settle(self, build_id: int, d: dict, row: dict) -> dict:
        """After 'done' the post-finish redeploy may still be starting and the test result not final. Poll until the app
        is running and the test is final (or nothing is declared), up to settle_seconds. Never fails the brief."""
        t0 = self.clock()
        declared: bool | None = None
        while True:
            app = d.get("app")
            app_ok = not isinstance(app, dict) or app.get("status") == "running"  # no app block: older API
            t = d.get("tests")
            if t is None:
                if declared is None:
                    declared = self._declares_test(build_id, d)
                tests_ok = not declared
            else:
                tests_ok = not isinstance(t, dict) or t.get("status") in TEST_FINAL
            waited = round(self.clock() - t0, 1)
            if app_ok and tests_ok:
                row["settle"] = {"settled": True, "seconds": waited}
                return d
            if self.clock() - t0 >= self.settle_seconds:
                why = []
                if not app_ok:
                    why.append(f"app {app.get('status')}")
                if not tests_ok:
                    why.append("test " + (str(t.get("status")) if isinstance(t, dict) else "result missing"))
                row["settle"] = {"settled": False, "seconds": waited}
                row["notes"].append(f"not settled after {waited:.0f}s ({', '.join(why)})")
                return d
            self.sleep(self.poll_seconds)
            try:
                nd = self._detail(build_id)
            except (EvalError, httpx.HTTPError):
                continue
            self._record_poll(nd)
            if nd.get("status") == "done":
                d = nd

    @staticmethod
    def _fill(row: dict, d: dict) -> None:
        row["status"] = d.get("status", "error")
        runs = d.get("runs") or []
        row["runs"] = len(runs)
        row["steps"] = sum(int(r.get("step") or 0) for r in runs)
        row["cost"] = float(d.get("cost_usd") or (d.get("spend") or {}).get("build_usd") or 0)
        t = d.get("tests")
        row["tests"] = t.get("status", "-") if isinstance(t, dict) else "none"
        verdicts = [r["review"]["verdict"] for r in runs if isinstance(r.get("review"), dict)]
        row["review"] = verdicts[0] if verdicts else "-"  # runs are newest first

    def _run_checks(self, row: dict, checks: list[dict], base: str) -> list[dict]:
        records = []
        for c in checks:
            rec = evaluate_check_detail(self.check_client, base, c)
            records.append(rec)
            row["checks_passed"] += rec["passed"]
            if not rec["passed"]:
                row["notes"].append(rec["reason"])
        return records

    def _run_flow(self, row: dict, steps: list[dict], base: str) -> list[dict]:
        """An ordered flow sharing one cookie jar (the check client's). Steps may use {user}/{password} and captured
        variables; a step with "fresh": true first drops the cookies (a new visitor)."""
        if not steps:
            return []
        vars: dict = {"user": "eval-" + secrets.token_hex(4), "password": "eval-" + secrets.token_hex(8)}
        records = []
        self.check_client.cookies.clear()
        try:
            for step in steps:
                if step.get("fresh"):
                    self.check_client.cookies.clear()
                c = subst({k: v for k, v in step.items() if k != "fresh"}, vars)
                c.setdefault("retry", c["method"] in ("GET", "HEAD"))
                rec = evaluate_check_detail(self.check_client, base, c)
                vars.update(rec.get("captured") or {})
                records.append(rec)
                row["checks_passed"] += rec["passed"]
                if not rec["passed"]:
                    row["notes"].append("flow " + rec["reason"])
        finally:
            self.check_client.cookies.clear()
        return records

    # -- all briefs
    def run_all(self, briefs: list[dict]) -> tuple[list[dict], float]:
        rows, spent = [], 0.0
        for b in briefs:
            if spent >= self.max_usd:
                row = new_row(b, self.model_used)
                row["status"] = "skipped"
                row["notes"].append(f"spend guard: ${spent:.3f} >= ${self.max_usd:.2f}")
                rows.append(row)
                continue
            row = self.run_brief(b)
            spent += row["cost"]
            rows.append(row)
        return rows, spent


def _fmt_counts(d: dict) -> str:
    return " ".join(f"{k}:{v}" for k, v in sorted(d.items())) or "-"


def _fmt_settle(s: dict | None) -> str:
    if not s:
        return "-"
    return f"{s['seconds']:.0f}s" if s.get("settled") else f"not settled ({s['seconds']:.0f}s)"


def render_table(rows: list[dict], *, date: str = "", model_note: str = "") -> str:
    out = []
    if date or model_note:
        out.append(f"### Kaisen eval {date} {model_note}".rstrip())
        out.append("")
    out.append("| Brief | Model | Status | Runs | Steps | Cost | Time | Tests | Checks | Review | Notes |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        notes = "; ".join(r["notes"]).replace("|", "/")
        out.append(f"| {r['name']} | {r.get('model') or '-'} | {r['status']} | {r['runs']} | {r['steps']} | ${r['cost']:.3f} "
                   f"| {r['minutes']:.1f} min | {r['tests']} | {r['checks_passed']}/{r['checks_total']} | {r['review']} | {notes} |")
    ran = [r for r in rows if r["status"] != "skipped"]
    done = sum(r["status"] == "done" for r in rows)
    cp, ct = sum(r["checks_passed"] for r in rows), sum(r["checks_total"] for r in ran)
    out.append("")
    out.append(f"Totals: {done}/{len(rows)} done, cost ${sum(r['cost'] for r in rows):.3f}, "
               f"{sum(r['minutes'] for r in rows):.1f} min, {sum(r['steps'] for r in rows)} steps, checks {cp}/{ct}.")
    behaving = [r for r in rows if r.get("metrics")]
    if behaving:
        out += ["", "#### Behaviour", "",
                "| Brief | Model | Tool calls | WARNINGs | Deploy not OK | HTTP statuses | Stops (repeat/max_tokens/turns) "
                "| Ctx resets | 1st deploy OK | Run durations (s) | Files / bytes | Tests / README | USER | PLAN done/open | kaisen.json "
                "| http_request paths | Authed reqs | PLAN gate hits | Settle |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in behaving:
            m, c = r["metrics"], r.get("code") or {}
            tools = " ".join(f"{k}:{v}" for k, v in m["tool_calls"].items() if v) or "-"
            if m.get("other_tool_calls"):
                tools += f" other:{m['other_tool_calls']}"
            for k in ("web_searches", "web_fetches"):
                if m.get(k):
                    tools += f" {k}:{m[k]}"
            fd =r.get("first_deploy_ok_min")
            plan = "-" if c.get("plan_ticked") is None else f"{c['plan_ticked']}/{c['plan_unticked']}"
            kj = c.get("kaisen_json")
            kjs = "-" if kj is None else json.dumps(kj, separators=(",", ":"))[:60]
            out.append(
                f"| {r['name']} | {r.get('model') or '-'} | {tools} | {m['write_warnings']} | {m['deploy_not_ok']} "
                f"| {_fmt_counts(m['http_status'])} | {m['repeat_stops']}/{m['max_tokens_stops']}/{m['turn_limit_stops']} "
                f"| {m['context_resets']} | {'-' if fd is None else f'{fd:.1f} min'} "
                f"| {','.join('?' if x is None else f'{x:.0f}' for x in r.get('run_seconds') or []) or '-'} "
                f"| {c.get('files', '-')} / {c.get('bytes', '-')} "
                f"| {'yes' if c.get('has_tests') else 'no'} / {'yes' if c.get('has_readme') else 'no'} "
                f"| {c.get('dockerfile_user') if c.get('dockerfile_user') is not None else '-'} | {plan} | {kjs.replace('|', '/')} "
                f"| {_fmt_counts(m.get('http_paths') or {})} | {m.get('http_authed', 0)} | {m.get('plan_gate', 0)} "
                f"| {_fmt_settle(r.get('settle'))} |")
    return "\n".join(out) + "\n"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manual Kaisen eval over the starter briefs (spends real money; never in CI).")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--briefs", default="all", help='comma-separated brief names or slugs, in run order, or "all"')
    p.add_argument("--token-env", default="E2E_USER_TOKEN", help="env var holding the API token")
    p.add_argument("--max-usd", type=float, default=1.00, help="stop starting new briefs once this much is spent")
    p.add_argument("--out", help="markdown file to append the results table to (default: stdout)")
    p.add_argument("--keep", action="store_true", help="do not delete builds and previews afterwards")
    p.add_argument("--data-dir", help="where to save per-brief data (default: <scratch-dir>/eval-data/<timestamp>)")
    p.add_argument("--scratch-dir", default=".", help="base for the default data dir")
    p.add_argument("--settle-seconds", type=float, default=SETTLE_SECONDS,
                   help="after done, wait up to this long for the app to run and the test result to be final")
    p.add_argument("--model", help="set the project's Kaisen model for this run; restored afterwards")
    p.add_argument("--effort", help="set the project's Kaisen effort for this run; restored afterwards")
    p.add_argument("--briefs-file", default=str(BRIEFS_FILE), help="JSON list of briefs (default: the starter briefs)")
    p.add_argument("--web", choices=("on", "off"), help="turn the project's Kaisen web access on/off for this run; restored afterwards")
    p.add_argument("--web-domains", help="comma-separated web allow-list for this run (empty string = any domain); restored afterwards")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    token = os.environ.get(a.token_env, "")
    if not token:
        print(f"error: env var {a.token_env} is empty or unset", file=sys.stderr)
        return 2
    names = None if a.briefs.strip().lower() == "all" else [n.strip() for n in a.briefs.split(",") if n.strip()]
    try:
        briefs = load_briefs(Path(a.briefs_file), names)
    except EvalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    data_dir = Path(a.data_dir) if a.data_dir else Path(a.scratch_dir) / "eval-data" / time.strftime("%Y%m%d-%H%M%S")
    restored = True
    with httpx.Client(base_url=a.base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60) as api, \
            httpx.Client(timeout=20, follow_redirects=False) as chk:
        ev = Evaluator(api, chk, project_id=a.project_id, max_usd=a.max_usd, keep=a.keep, data_dir=data_dir,
                       model=a.model, effort=a.effort, scrub=[token],
                       web=None if a.web is None else a.web == "on",
                       web_domains=None if a.web_domains is None else [d.strip() for d in a.web_domains.split(",") if d.strip()],
                       settle_seconds=a.settle_seconds)
        t0 = time.monotonic()
        try:
            ev.apply_settings()
            rows, _ = ev.run_all(briefs)
        except EvalError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        finally:
            restored = ev.restore_settings()
        wall = round(time.monotonic() - t0, 1)
    date = time.strftime("%Y-%m-%d")
    table = render_table(rows, date=date, model_note=f"(project {a.project_id}, model {ev.model_used or '?'})")
    summary = {"date": date, "project_id": a.project_id, "model": ev.model_used, "effort": a.effort, "web": a.web,
               "wall_seconds": wall, "settings_restored": restored, "rows": rows}
    ev._write_json("summary.json", summary)
    ev._write("summary.md", table)
    print(f"data saved in {data_dir}", file=sys.stderr)
    if a.out:
        with open(a.out, "a") as f:
            f.write("\n" + table)
    else:
        print(table)
    return 0 if all(r["status"] == "done" for r in rows) and restored else 1


if __name__ == "__main__":
    sys.exit(main())
