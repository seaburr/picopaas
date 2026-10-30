import gzip
import io
import re
import tarfile

from app import config
from .paths import normalize_path


def build_tarball(slug: str, files: list[tuple[str, str]]) -> bytes:
    """Deterministic tar.gz of (path, content) under a `<slug>/` directory. Held in memory."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path, content in sorted(files):
                safe = normalize_path(path)  # re-check: never trust stored names
                data = content.encode()
                info = tarfile.TarInfo(f"{slug}/{safe}")
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


CONVERSATION_FILE = "CONVERSATION.md"
_BRIEF_RE = re.compile(r"<brief>\n(.*?)\n</brief>", re.S)
_FEEDBACK_RE = re.compile(r"<feedback>\n(.*?)\n</feedback>", re.S)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines()) or ">"


def build_conversation(name: str, brief: str, runs: list, secrets) -> str:
    """Readable Markdown of the human's conversation with Kaisen, for the download only: what the person wrote
    (brief, change requests, answers) and what Kaisen said to them (its text, its questions, the run summary/error).
    No system prompt, tool calls/results, thinking, file contents or internal ids. Redacted like the transcript view."""
    from .redact import redact

    def r(t: str) -> str:
        return redact(t, secrets)

    out = [f"# Conversation: {name}", "", "What you asked for and what Kaisen said back, run by run. "
           "Tool calls, thinking and file contents are not included.", ""]
    prev_len = 0
    for n, run in enumerate(runs, 1):
        finished = run.finished_at.strftime("%Y-%m-%d %H:%M UTC") if run.finished_at else "not finished"
        out += [f"## Run {n} ({run.kind})", "",
                f"Started {run.created_at.strftime('%Y-%m-%d %H:%M UTC')}, finished {finished}, cost ${run.cost_usd or 0.0:.2f}, "
                f"status {run.status}.", ""]
        msgs = list(run.messages or [])
        if not msgs and run.status in ("done", "stopped", "error"):
            out += [f"_The conversation text of this run was removed after {config.KAISEN_TRANSCRIPT_DAYS} days._", ""]
            if run.kind == "build" and n == 1:
                out += ["### You", "", _quote(r(brief)), ""]
        else:
            body = msgs[prev_len:] if run.kind == "resume" else msgs  # a resume repeats the earlier history first
            ask_ids: set[str] = set()
            for i, m in enumerate(body):
                content = m.get("content")
                if m.get("role") == "user":
                    if isinstance(content, str):
                        if i == 0 and run.kind == "build":
                            mm = _BRIEF_RE.search(content)
                            out += ["### You", "", _quote(r((mm.group(1) if mm else brief).replace("<\\/brief", "</brief"))), ""]
                        elif i == 0 and run.kind == "feedback":
                            mm = _FEEDBACK_RE.search(content)
                            if mm:
                                out += ["### You (change request)", "", _quote(r(mm.group(1).replace("<\\/feedback", "</feedback"))), ""]
                        continue
                    for blk in content or []:
                        if blk.get("type") == "tool_result" and blk.get("tool_use_id") in ask_ids:
                            c = blk.get("content")
                            out += ["### You (answer)", "", _quote(r(c if isinstance(c, str) else "")), ""]
                elif m.get("role") == "assistant":
                    for blk in content if isinstance(content, list) else []:
                        t = blk.get("type")
                        if t == "text" and (blk.get("text") or "").strip():
                            out += ["### Kaisen", "", r(blk["text"].strip()), ""]
                        elif t == "tool_use" and blk.get("name") == "ask_human":
                            ask_ids.add(blk.get("id"))
                            out += ["### Kaisen (question)", "", _quote(r(str((blk.get("input") or {}).get("question", "")))), ""]
        if run.summary:
            out += ["### Kaisen (summary)", "", r(run.summary.strip()), ""]
        if run.error:
            out += ["### Kaisen (error)", "", r(run.error.strip()), ""]
        if run.messages:
            prev_len = len(run.messages)
    return "\n".join(out).rstrip() + "\n"
