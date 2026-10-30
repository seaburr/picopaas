"""Write-time checks for files the builder writes (design s15, H2). They only parse text: nothing is ever executed.
A problem becomes a WARNING line appended to the write_file / edit_file result; the write itself still succeeds."""
import ast
import json
import re
from re import _constants as _c, _parser

from app import config

from . import deploy

RUNTIMES = ("static", "python", "node")
MANIFEST_KEYS = ("runtime", "start", "health", "database", "bucket", "volume", "test")


def _manifest_problems(data) -> list[str]:
    if not isinstance(data, dict):
        return ["kaisen.json must be a JSON object"]
    out = []
    unknown = sorted(set(data) - set(MANIFEST_KEYS))
    if unknown:
        out.append(f"kaisen.json has unknown key(s): {', '.join(map(str, unknown))} (known: {', '.join(MANIFEST_KEYS)})")
    runtime = data.get("runtime", "static")
    if runtime not in RUNTIMES:
        out.append(f'kaisen.json "runtime" must be one of {", ".join(RUNTIMES)} (got {runtime!r})')
    start = data.get("start")
    if start is not None and not isinstance(start, str):
        out.append('kaisen.json "start" must be a command string')
    elif runtime == "python" and not (start or "").strip():
        out.append('kaisen.json: the "python" runtime requires a "start" command')
    health = data.get("health")
    if health is not None and not (isinstance(health, str) and health.startswith("/")):
        out.append('kaisen.json "health" must be a path starting with "/"')
    try:
        deploy.wanted_from_manifest(data)  # database / bucket / volume / test: the same rules the deploy applies
    except deploy.DeployError as e:
        out.append(str(e))
    return out


def check(path: str, content: str) -> str:
    """"" when the file looks fine, else one "\\nWARNING: ..." line per problem."""
    problems: list[str] = []
    if path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as e:
            problems.append(f"Python syntax error at line {e.lineno}: {e.msg}")
        except (ValueError, RecursionError, MemoryError) as e:  # null bytes; a 100k-term expression overflows the stack
            problems.append(f"Python source cannot be parsed: {e or type(e).__name__}")
    elif path.endswith(".json"):
        try:
            data = json.loads(content)
        except ValueError as e:
            problems.append(f"invalid JSON at line {getattr(e, 'lineno', '?')}: {getattr(e, 'msg', e)}")
        else:
            if path == "kaisen.json":
                problems += _manifest_problems(data)
    return "".join(f"\nWARNING: {p}" for p in problems)


# ---- search_files regex guard (NEEDS-SECURITY-REVIEW K23) ------------------------------------------------------
# Python's re has no timeout and the worker is one process, so a model-chosen pattern must be rejected up front unless
# its worst case is polynomial and small. We parse it (nothing is matched here) and refuse: a repeat that contains a
# repeat or an alternation, back-references and look-arounds, and more than MAX_VARIABLE_REPEATS variable repeats.
MAX_VARIABLE_REPEATS = 3
_REPEATS = (_c.MAX_REPEAT, _c.MIN_REPEAT, getattr(_c, "POSSESSIVE_REPEAT", _c.MAX_REPEAT))


def _walk(items, in_repeat: bool, count: list[int]) -> str | None:
    for op, av in items:
        if op in _REPEATS:
            lo, hi, body = av
            if lo != hi:
                count[0] += 1
            if in_repeat and lo != hi:
                return "a repeat inside a repeat"
            bad = _walk(body, in_repeat or hi > 1, count)
        elif op is _c.BRANCH:
            if in_repeat:
                return "an alternation inside a repeat"
            bad = next(filter(None, (_walk(b, in_repeat, count) for b in av[1])), None)
        elif op in (_c.GROUPREF, _c.GROUPREF_EXISTS):
            return "a back-reference"
        elif op in (_c.ASSERT, _c.ASSERT_NOT):
            return "a look-around"
        elif op is _c.SUBPATTERN:
            bad = _walk(av[3], in_repeat, count)
        elif op is _c.ATOMIC_GROUP:
            bad = _walk(av, in_repeat, count)
        else:
            bad = None
        if bad:
            return bad
    return None


def regex_problem(pattern: str) -> str | None:
    """None when the pattern is safe to run against model-chosen file content, else a reason the model can act on."""
    try:
        tree = _parser.parse(pattern)
    except (re.error, RecursionError, OverflowError) as e:
        return f"invalid regex: {e}"
    count = [0]
    bad = _walk(tree, False, count)
    if bad is None and count[0] > MAX_VARIABLE_REPEATS:
        bad = f"more than {MAX_VARIABLE_REPEATS} variable repeats (*, +, ?, {{m,n}})"
    return None if bad is None else f"regex rejected: {bad} can run for minutes; simplify it or use a plain-text search"
