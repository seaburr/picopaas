"""The package steward's `latest_versions` tool: deterministic registry lookups, no model, no metering.

NEEDS-SECURITY-REVIEW: with httptool this is the only place the worker sends model-chosen bytes over the network.
- Hosts are fixed (config.KAISEN_PACKAGES_NPM_URL / _PYPI_URL); the model supplies only a validated package NAME that
  becomes one URL path segment. Redirects are never followed, proxies from the environment are ignored.
- Answers are byte-capped and parsed, never echoed. What reaches the model is a handful of fields that each passed a
  strict regex (versions, an ISO date) plus booleans and a status word. Registry text (descriptions, READMEs,
  deprecation messages, engine ranges) is read by this code and dropped, so a hostile package page cannot inject
  anything into a model, including a builder that was denied web access because the build has secrets (pentest K16).
- Not found is `unknown` (typo or hijack bait: the agent flags it); any network trouble is `unavailable`."""
import json
import re
import time

import httpx

from app import config
from .paths import ToolError

NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
PYPI_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")  # PEP 503
NPM_VERSION = re.compile(r"^(?:0|[1-9]\d{0,8})\.(?:0|[1-9]\d{0,8})\.(?:0|[1-9]\d{0,8})$", re.ASCII)  # stable only: no -pre, no +build
PYPI_VERSION = re.compile(r"^(?:0|[1-9]\d{0,8})(?:\.(?:0|[1-9]\d{0,8})){1,3}$", re.ASCII)  # stable only: no a/b/rc/dev/post
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})", re.ASCII)
ECOSYSTEMS = ("npm", "pypi")
_RANGE_MAX = 200  # engine / requires_python strings longer than this are treated as "no constraint"


def valid_name(ecosystem: str, name) -> str | None:
    """The canonical name when it is acceptable, else None (path tricks, URL-encoding, oversize all fail the regexes)."""
    if not isinstance(name, str):
        return None
    if ecosystem == "npm":
        return name if len(name) <= config.KAISEN_PACKAGES_NAME_MAX and NPM_NAME.fullmatch(name) else None
    if ecosystem == "pypi":
        if len(name) > 100 or not PYPI_NAME.fullmatch(name):
            return None
        return re.sub(r"[-_.]+", "-", name).lower()
    return None


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _pad(t: tuple[int, ...], n: int = 3) -> tuple[int, ...]:
    return (t + (0,) * n)[:max(n, len(t))]


# ---- npm "engines.node" ranges (a pragmatic semver subset; anything not understood counts as compatible) -----------

_NPM_PART = re.compile(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def _npm_partial(tok: str):
    m = _NPM_PART.match(tok)
    if not m:
        raise ValueError(tok)
    parts = []
    for g in m.groups():
        if g is None or g in "xX*":
            break
        parts.append(int(g))
    return parts


def _bump(parts: list[int]) -> tuple[int, int, int]:
    p = parts[:]
    p[-1] += 1
    return _pad(tuple(p))  # type: ignore[return-value]


def _npm_comparators(op: str, parts: list[int]):
    full = _pad(tuple(parts))
    if not parts:
        return []
    if op in ("", "="):
        return [("=", full)] if len(parts) == 3 else [(">=", full), ("<", _bump(parts))]
    if op == ">":
        return [(">", full)] if len(parts) == 3 else [(">=", _bump(parts))]
    if op == ">=":
        return [(">=", full)]
    if op == "<":
        return [("<", full)]
    if op == "<=":
        return [("<=", full)] if len(parts) == 3 else [("<", _bump(parts))]
    if op == "~":
        return [(">=", full), ("<", _bump(parts[:1] if len(parts) == 1 else parts[:2]))]
    if op == "^":
        k = next((i for i, x in enumerate(parts) if x != 0), len(parts) - 1)
        return [(">=", full), ("<", _bump(parts[:k + 1]))]
    raise ValueError(op)


def _cmp(op: str, a: tuple, b: tuple) -> bool:
    return {"=": a == b, ">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def node_ok(engine: str | None, major: int | None = None) -> bool:
    """Does `engines.node` admit the runtime (Node `major`.9999.9999)? Unknown syntax means "yes": never block on a guess."""
    if not engine or len(engine) > _RANGE_MAX:
        return True
    runtime = (major if major is not None else config.KAISEN_RUNTIME_NODE_MAJOR, 9999, 9999)
    try:
        for alt in engine.split("||"):
            toks = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", alt.strip()).split()
            comps: list = []
            i = 0
            while i < len(toks):
                if i + 2 < len(toks) and toks[i + 1] == "-":  # hyphen range "a - b"
                    comps += _npm_comparators(">=", _npm_partial(toks[i])) + _npm_comparators("<=", _npm_partial(toks[i + 2]))
                    i += 3
                    continue
                m = re.match(r"^(>=|<=|>|<|=|\^|~)?(.*)$", toks[i])
                comps += _npm_comparators(m.group(1) or "", _npm_partial(m.group(2)))
                i += 1
            if all(_cmp(op, runtime, v) for op, v in comps):
                return True
        return False
    except (ValueError, IndexError, AttributeError):
        return True


# ---- PyPI "requires_python" (PEP 440 specifier subset) ---------------------------------------------------------------

_PY_SPEC = re.compile(r"^(~=|==|!=|>=|<=|>|<)\s*(\d+(?:\.\d+)*)(\.\*)?$")


def python_ok(spec: str | None, runtime: tuple[int, ...] | None = None) -> bool:
    if not spec or len(spec) > _RANGE_MAX:
        return True
    rt = runtime or config.KAISEN_RUNTIME_PYTHON
    try:
        for part in spec.split(","):
            m = _PY_SPEC.match(part.strip())
            if not m:
                return True
            op, ver, star = m.group(1), tuple(int(x) for x in m.group(2).split(".")), m.group(3)
            n = max(len(ver), 3)
            a, b = _pad(rt, n), _pad(ver, n)
            if op in ("==", "!=") and star:
                same = _pad(rt, len(ver))[:len(ver)] == ver
                if same != (op == "=="):
                    return False
            elif op == "~=":
                keep = ver[:-1] if len(ver) > 1 else ver
                if not (a >= b and _pad(rt, len(keep))[:len(keep)] == keep):
                    return False
            elif not {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]:
                return False
        return True
    except ValueError:
        return True


# ---- registry documents -> a few validated fields --------------------------------------------------------------------

def _result(latest: str | None, published: str | None, compat: dict[str, bool]) -> dict:
    ok_versions = [v for v, good in compat.items() if good]
    best_ok = max(ok_versions, key=vtuple) if ok_versions else None
    return {"status": "ok", "latest": latest, "published": published,
            "compatible": bool(compat.get(latest)), "latest_compatible": best_ok}


def parse_npm(doc) -> dict:
    versions = doc.get("versions") if isinstance(doc, dict) else None
    if not isinstance(versions, dict):
        return {"status": "unavailable"}
    compat: dict[str, bool] = {}
    for v, meta in versions.items():
        if not isinstance(v, str) or not NPM_VERSION.fullmatch(v) or not isinstance(meta, dict) or meta.get("deprecated"):
            continue
        eng = meta.get("engines") if isinstance(meta.get("engines"), dict) else {}
        node = eng.get("node") if isinstance(eng.get("node"), str) else None
        compat[v] = node_ok(node)
    if not compat:
        return {"status": "unknown"}
    latest = max(compat, key=vtuple)
    return _result(latest, None, compat)  # the install-v1 document carries no publish dates


def parse_pypi(doc) -> dict:
    releases = doc.get("releases") if isinstance(doc, dict) else None
    if not isinstance(releases, dict):
        return {"status": "unavailable"}
    compat: dict[str, bool] = {}
    dates: dict[str, str] = {}
    for v, files in releases.items():
        if not isinstance(v, str) or not PYPI_VERSION.fullmatch(v) or not isinstance(files, list):
            continue
        files = [f for f in files if isinstance(f, dict) and not f.get("yanked")]
        if not files:
            continue
        rp = next((f.get("requires_python") for f in files if isinstance(f.get("requires_python"), str)), None)
        compat[v] = python_ok(rp)
        stamps = sorted(str(f.get("upload_time_iso_8601") or "") for f in files)
        m = _DATE.match(stamps[0]) if stamps else None
        if m:
            dates[v] = m.group(1)
    if not compat:
        return {"status": "unknown"}
    latest = max(compat, key=vtuple)
    return _result(latest, dates.get(latest), compat)


class PackageIndex:
    """Registry client. `transport` lets tests supply httpx.MockTransport; production uses the real network."""

    def __init__(self, transport=None, clock=time.monotonic):
        self._transport, self._clock = transport, clock
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self.requests = 0

    def _url(self, ecosystem: str, name: str) -> str:
        if ecosystem == "npm":
            return config.KAISEN_PACKAGES_NPM_URL + name.replace("/", "%2F")
        return f"{config.KAISEN_PACKAGES_PYPI_URL}{name}/json"

    def _get(self, url: str, accept: str) -> tuple[int, bytes | None]:
        """(status, body); body None when it is not JSON-sized or the transport failed (status 0)."""
        timeout = httpx.Timeout(config.KAISEN_PACKAGES_READ_SECONDS, connect=config.KAISEN_PACKAGES_CONNECT_SECONDS)
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False, trust_env=False, timeout=timeout,
                              headers={"Accept": accept, "User-Agent": "picopaas-kaisen-package-steward"}) as c:
                with c.stream("GET", url) as r:
                    if r.status_code != 200:
                        return r.status_code, None
                    buf = bytearray()
                    deadline = self._clock() + config.KAISEN_PACKAGES_TOTAL_SECONDS  # a slow drip never trips the per-read timeout
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > config.KAISEN_PACKAGES_RESPONSE_MAX or self._clock() > deadline:
                            return 200, None
                    return 200, bytes(buf)
        except (httpx.HTTPError, OSError, ValueError):
            return 0, None

    def lookup(self, ecosystem: str, name: str) -> dict:
        """One validated name -> {ecosystem, name, status, ...}. Never raises."""
        key = (ecosystem, name)
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < config.KAISEN_PACKAGES_CACHE_SECONDS:
            return hit[1]
        self.requests += 1
        accept = "application/vnd.npm.install-v1+json" if ecosystem == "npm" else "application/json"
        status, body = self._get(self._url(ecosystem, name), accept)
        if status == 404:
            res = {"status": "unknown"}
        elif status != 200 or body is None:
            res = {"status": "unavailable"}
        else:
            try:
                doc = json.loads(body)
                res = parse_npm(doc) if ecosystem == "npm" else parse_pypi(doc)
            except ValueError:
                res = {"status": "unavailable"}
        res = {"ecosystem": ecosystem, "name": name, **res}
        if res["status"] != "unavailable":  # a failure is retried, not remembered
            self._cache[key] = (self._clock(), res)
        return res

    def latest_versions(self, items, on_item=None) -> list[dict]:
        """The tool body. `items` = [{ecosystem, name}], at most KAISEN_PACKAGES_BATCH_MAX. Invalid entries come back as
        {"status": "invalid"} without echoing the text. `on_item` is called before each network lookup (watchdog stamp)."""
        if not isinstance(items, list) or not items:
            raise ToolError("'packages' must be a non-empty list of {ecosystem, name}")
        if len(items) > config.KAISEN_PACKAGES_BATCH_MAX:
            raise ToolError(f"at most {config.KAISEN_PACKAGES_BATCH_MAX} packages per call")
        out = []
        for it in items:
            eco = it.get("ecosystem") if isinstance(it, dict) else None
            name = valid_name(eco, it.get("name")) if eco in ECOSYSTEMS else None
            if name is None:
                out.append({"status": "invalid"})
                continue
            if on_item:
                on_item()
            out.append(self.lookup(eco, name))
        return out


def render(results: list[dict]) -> str:
    """Compact, free-text-free tool result for the model."""
    return json.dumps([{k: v for k, v in r.items() if v is not None} for r in results], separators=(",", ":"))
