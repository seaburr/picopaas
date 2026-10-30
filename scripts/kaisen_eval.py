#!/usr/bin/env python3
"""Manual Kaisen eval (design s15 H6). NEVER run in CI: it spends real money on the project's Anthropic key.

Drives the public PicoPaaS API with an API token: for each brief it creates a build, starts it, polls until the build
settles, runs HTTP checks against the preview and prints a markdown table (design s14 style) plus totals.

    E2E_USER_TOKEN=... python scripts/kaisen_eval.py --project-id 123 --briefs "Habit tracker" --max-usd 0.50

The token is read from an environment variable (--token-env) and is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "https://platform.picopaas.xyz"
BRIEFS_FILE = Path(__file__).with_name("kaisen_eval_briefs.json")
POLL_SECONDS = 10
TIMEOUT_SECONDS = 20 * 60
ASSUMPTION_ANSWER = "Decide yourself and note the assumption."
NO_RESUME_WORDS = ("cap", "refus")  # a spend cap or refusal will not fix itself on resume
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


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
        checks = b.get("checks")
        if not isinstance(checks, list) or not checks:
            raise EvalError(f"brief {name!r}: checks must be a non-empty list")
        for c in checks:
            if not isinstance(c, dict) or c.get("method") not in METHODS or not str(c.get("path", "")).startswith("/"):
                raise EvalError(f"brief {name!r}: each check needs a method and a path starting with /")
            exp = c.get("expect_status")
            if not (isinstance(exp, int) or (isinstance(exp, list) and exp and all(isinstance(x, int) for x in exp))):
                raise EvalError(f"brief {name!r}: check {c['path']} needs expect_status (int or list of ints)")
    if names:
        by = {b["name"].lower(): b for b in data}
        missing = [n for n in names if n.lower() not in by]
        if missing:
            raise EvalError(f"unknown brief(s): {', '.join(missing)}; known: {', '.join(b['name'] for b in data)}")
        return [by[n.lower()] for n in names]
    return data


def evaluate_check(client: httpx.Client, base: str, check: dict) -> tuple[bool, str]:
    """Run one HTTP check against the preview. Returns (passed, reason)."""
    kwargs = {}
    if "body" in check:
        kwargs["json"] = check["body"]
    resp = None
    err = ""
    for attempt in range(3):  # a fresh preview can need a moment
        try:
            resp = client.request(check["method"], base + check["path"], **kwargs)
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}"
            resp = None
        if resp is not None and resp.status_code < 500:
            break
        if attempt < 2:
            time.sleep(CHECK_RETRY_SLEEP)
    if resp is None:
        return False, f"{check['method']} {check['path']}: request failed ({err})"
    exp = check["expect_status"]
    exp = exp if isinstance(exp, list) else [exp]
    if resp.status_code not in exp:
        return False, f"{check['method']} {check['path']}: status {resp.status_code}, wanted {exp}"
    want = check.get("expect_contains")
    if want and want not in resp.text:
        return False, f"{check['method']} {check['path']}: body lacks {want!r}"
    return True, "ok"


CHECK_RETRY_SLEEP = 5


def preview_base(detail: dict) -> str | None:
    host = detail.get("app_hostname") or (detail.get("app") or {}).get("hostname")
    if not host:
        return None
    return host.rstrip("/") if "://" in host else f"https://{host}"


class Evaluator:
    def __init__(self, api: httpx.Client, check_client: httpx.Client, *, project_id: int, max_usd: float,
                 keep: bool = False, sleep=time.sleep, clock=time.monotonic,
                 poll_seconds: float = POLL_SECONDS, timeout_seconds: float = TIMEOUT_SECONDS, log=None):
        self.api, self.check_client = api, check_client
        self.project_id, self.max_usd, self.keep = project_id, max_usd, keep
        self.sleep, self.clock = sleep, clock
        self.poll_seconds, self.timeout_seconds = poll_seconds, timeout_seconds
        self.log = log or (lambda m: print(m, file=sys.stderr))

    # -- API helpers
    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.api.request(method, path, **kw)
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
    def wait_settled(self, build_id: int, state: dict) -> dict:
        """Poll until done/stopped/error. Answers a waiting question once, resumes a stop/error once."""
        deadline = self.clock() + self.timeout_seconds
        while True:
            d = self._detail(build_id)
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
        row = {"name": brief["name"], "status": "error", "runs": 0, "steps": 0, "cost": 0.0, "minutes": 0.0,
               "tests": "-", "checks_passed": 0, "checks_total": len(brief["checks"]), "review": "-", "notes": []}
        start = self.clock()
        build_id = None
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
            d = self.wait_settled(build_id, state)
            row["minutes"] = round((self.clock() - start) / 60, 1)
            self._fill(row, d)
            if state["answered"]:
                row["notes"].append("answered ask_human")
            if state["resumed"]:
                row["notes"].append("resumed once")
            if d["status"] == "done":
                base = preview_base(d)
                if base:
                    self._run_checks(row, brief["checks"], base)
                else:
                    row["notes"].append("no preview hostname")
        except EvalError as e:
            row["notes"].append(str(e))
        finally:
            if build_id is not None and not self.keep:
                try:
                    self._req("DELETE", f"/kaisen/builds/{build_id}", params={"delete_app": "true"})
                except (EvalError, httpx.HTTPError) as e:
                    row["notes"].append(f"cleanup failed: {e}")
        return row

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

    def _run_checks(self, row: dict, checks: list[dict], base: str) -> None:
        for c in checks:
            ok, why = evaluate_check(self.check_client, base, c)
            row["checks_passed"] += ok
            if not ok:
                row["notes"].append(why)

    # -- all briefs
    def run_all(self, briefs: list[dict]) -> tuple[list[dict], float]:
        rows, spent = [], 0.0
        for b in briefs:
            if spent >= self.max_usd:
                rows.append({"name": b["name"], "status": "skipped", "runs": 0, "steps": 0, "cost": 0.0, "minutes": 0.0,
                             "tests": "-", "checks_passed": 0, "checks_total": len(b["checks"]), "review": "-",
                             "notes": [f"spend guard: ${spent:.3f} >= ${self.max_usd:.2f}"]})
                continue
            row = self.run_brief(b)
            spent += row["cost"]
            rows.append(row)
        return rows, spent


def render_table(rows: list[dict], *, date: str = "", model_note: str = "") -> str:
    out = []
    if date or model_note:
        out.append(f"### Kaisen eval {date} {model_note}".rstrip())
        out.append("")
    out.append("| Brief | Status | Runs | Steps | Cost | Time | Tests | Checks | Review | Notes |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        notes = "; ".join(r["notes"]).replace("|", "/")
        out.append(f"| {r['name']} | {r['status']} | {r['runs']} | {r['steps']} | ${r['cost']:.3f} | {r['minutes']:.1f} min "
                   f"| {r['tests']} | {r['checks_passed']}/{r['checks_total']} | {r['review']} | {notes} |")
    ran = [r for r in rows if r["status"] != "skipped"]
    done = sum(r["status"] == "done" for r in rows)
    cp, ct = sum(r["checks_passed"] for r in rows), sum(r["checks_total"] for r in ran)
    out.append("")
    out.append(f"Totals: {done}/{len(rows)} done, cost ${sum(r['cost'] for r in rows):.3f}, "
               f"{sum(r['minutes'] for r in rows):.1f} min, {sum(r['steps'] for r in rows)} steps, checks {cp}/{ct}.")
    return "\n".join(out) + "\n"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manual Kaisen eval over the starter briefs (spends real money; never in CI).")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--project-id", type=int, required=True)
    p.add_argument("--briefs", default="all", help='comma-separated brief names, or "all"')
    p.add_argument("--token-env", default="E2E_USER_TOKEN", help="env var holding the API token")
    p.add_argument("--max-usd", type=float, default=1.00, help="stop starting new briefs once this much is spent")
    p.add_argument("--out", help="markdown file to append the results table to (default: stdout)")
    p.add_argument("--keep", action="store_true", help="do not delete builds and previews afterwards")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    token = os.environ.get(a.token_env, "")
    if not token:
        print(f"error: env var {a.token_env} is empty or unset", file=sys.stderr)
        return 2
    names = None if a.briefs.strip().lower() == "all" else [n.strip() for n in a.briefs.split(",") if n.strip()]
    try:
        briefs = load_briefs(BRIEFS_FILE, names)
    except EvalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with httpx.Client(base_url=a.base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=30) as api, \
            httpx.Client(timeout=20, follow_redirects=False) as chk:
        ev = Evaluator(api, chk, project_id=a.project_id, max_usd=a.max_usd, keep=a.keep)
        rows, _ = ev.run_all(briefs)
    table = render_table(rows, date=time.strftime("%Y-%m-%d"), model_note=f"(project {a.project_id})")
    if a.out:
        with open(a.out, "a") as f:
            f.write("\n" + table)
    else:
        print(table)
    return 0 if all(r["status"] == "done" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
