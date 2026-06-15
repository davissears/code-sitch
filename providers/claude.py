"""
Claude Code provider adapter.

This module keeps Claude-specific transcript discovery, parsing, liveness
matching, chat reads, and resume command construction in one place. Public
metadata uses provider-qualified ids (`claude:<raw_session_id>`) while the raw
Claude id is preserved for CLI operations such as `claude --resume`.
"""

import glob
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone

import config

PROVIDER = "claude"
PROVIDER_LABEL = "Claude"
CAPABILITIES = {
    "chat": True,
    "resume": True,
    "live": True,
    "send": True,
    "focus": True,
}

PROJECTS_DIR = config.CLAUDE_PROJECTS_DIR

TAIL_BYTES = 1 << 20
_BIG_LINE = 20000

_SKIP_PROMPT_PREFIXES = (
    "<command-name>",
    "<local-command",
    "<command-message>",
    "Caveat:",
    "[Request interrupted",
)

_NOISE_PREFIXES = (
    "<command-name>", "<local-command", "<command-message>",
    "<system-reminder>", "Caveat:", "[Request interrupted",
)

_TOOL_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url",
                      "query", "description", "prompt", "skill", "subject")

_RE_IN = re.compile(rb'"input_tokens":\s*(\d+)')
_RE_OUT = re.compile(rb'"output_tokens":\s*(\d+)')
_RE_CC = re.compile(rb'"cache_creation_input_tokens":\s*(\d+)')
_RE_CR = re.compile(rb'"cache_read_input_tokens":\s*(\d+)')
_RE_TS = re.compile(rb'"timestamp":"([^"]+)"')
_RE_RID = re.compile(rb'"requestId":"([^"]+)"')

_IDLE_GLYPH = "✳"


def qualify_session_id(raw_session_id):
    raw = raw_session_id or ""
    return raw if raw.startswith(PROVIDER + ":") else PROVIDER + ":" + raw


def capabilities():
    return dict(CAPABILITIES)


def raw_session_id(meta_or_id):
    if isinstance(meta_or_id, dict):
        sid = meta_or_id.get("raw_session_id") or meta_or_id.get("session_id") or ""
    else:
        sid = meta_or_id or ""
    prefix = PROVIDER + ":"
    return sid[len(prefix):] if sid.startswith(prefix) else sid


def _first_int(regex, raw):
    m = regex.search(raw)
    return int(m.group(1)) if m else 0


def _iso_to_local_day(s):
    """Local calendar date (YYYY-MM-DD) for an ISO-8601 UTC timestamp, or None."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return None


def _local_day(raw):
    """Local day of a raw assistant line's UTC timestamp (regex, no full parse)."""
    m = _RE_TS.search(raw)
    return _iso_to_local_day(m.group(1).decode("ascii", "replace")) if m else None


def _text_from_message(message):
    """Pull plain text out of a user/assistant message.content (str or parts)."""
    if message is None:
        return ""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(part.get("text", ""))
        return "\n".join(chunks).strip()
    return ""


def _looks_like_real_prompt(text):
    if not text:
        return False
    for p in _SKIP_PROMPT_PREFIXES:
        if text.startswith(p):
            return False
    return True


def _project_label(cwd, fallback_dir):
    """Friendly project name: last two path components of the cwd."""
    if cwd:
        parts = [p for p in cwd.split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        if parts:
            return parts[-1]
    name = (fallback_dir or "").lstrip("-").replace("-", "/")
    return name or "(unknown)"


def parse_session(path):
    """Single-pass parse of one Claude JSONL transcript -> provider meta dict."""
    raw_id = os.path.splitext(os.path.basename(path))[0]
    project_dir = os.path.basename(os.path.dirname(path))
    try:
        st = os.stat(path)
    except OSError:
        return None

    ai_title = None
    last_prompt = None
    first_prompt = None
    cwd = None
    git_branch = None
    slug = None
    entrypoint = None
    first_ts = None
    n_user = 0
    n_assistant = 0
    samples = []
    have_meta = False
    usage_by_day = {}
    user_by_day = {}
    seen_req = set()

    try:
        with open(path, "rb") as f:
            for raw in f:
                big = len(raw) > _BIG_LINE

                if b'"type":"assistant"' in raw:
                    rid_m = _RE_RID.search(raw)
                    rid = rid_m.group(1) if rid_m else None
                    if rid is None or rid not in seen_req:
                        if rid is not None:
                            seen_req.add(rid)
                        n_assistant += 1
                        day = _local_day(raw)
                        if day:
                            b = usage_by_day.get(day)
                            if b is None:
                                b = usage_by_day[day] = [0, 0, 0, 0]
                            b[0] += _first_int(_RE_IN, raw)
                            b[1] += _first_int(_RE_OUT, raw)
                            b[2] += _first_int(_RE_CC, raw)
                            b[3] += _first_int(_RE_CR, raw)
                    if have_meta:
                        continue
                elif big and have_meta and not (b'"type":"user"' in raw and b'"toolUseResult"' not in raw):
                    continue

                try:
                    o = json.loads(raw)
                except Exception:
                    continue
                t = o.get("type")

                if t == "ai-title":
                    ai_title = o.get("aiTitle") or ai_title
                    continue
                if t == "last-prompt":
                    if o.get("lastPrompt"):
                        last_prompt = o["lastPrompt"]
                    continue

                if not have_meta:
                    cwd = cwd or o.get("cwd")
                    git_branch = git_branch or o.get("gitBranch")
                    slug = slug or o.get("slug")
                    entrypoint = entrypoint or o.get("entrypoint")
                    first_ts = first_ts or o.get("timestamp")
                    if entrypoint == "sdk-cli":
                        return None
                    if cwd:
                        have_meta = True

                if t == "assistant":
                    continue

                if t == "user":
                    if o.get("isMeta") or o.get("isSidechain") or o.get("isVisibleInTranscriptOnly"):
                        continue
                    if o.get("toolUseResult") is not None:
                        continue
                    text = _text_from_message(o.get("message"))
                    if _looks_like_real_prompt(text):
                        n_user += 1
                        day = _iso_to_local_day(o.get("timestamp"))
                        if day:
                            user_by_day[day] = user_by_day.get(day, 0) + 1
                        if first_prompt is None:
                            first_prompt = text
                        if len(samples) < 6:
                            samples.append(text[:400])
                    continue
    except OSError:
        return None

    title = ai_title or (first_prompt[:80] if first_prompt else None) or slug or "(untitled session)"

    return {
        "provider": PROVIDER,
        "provider_label": PROVIDER_LABEL,
        "capabilities": capabilities(),
        "session_id": qualify_session_id(raw_id),
        "raw_session_id": raw_id,
        "path": path,
        "project_dir": project_dir,
        "project": _project_label(cwd, project_dir),
        "cwd": cwd,
        "git_branch": git_branch,
        "slug": slug,
        "title": title,
        "ai_title": ai_title,
        "first_prompt": first_prompt,
        "last_prompt": last_prompt,
        "samples": samples,
        "messages": n_assistant,
        "user_turns": n_user,
        "created": first_ts,
        "updated": st.st_mtime,
        "size": st.st_size,
        "usage_by_day": usage_by_day,
        "user_by_day": user_by_day,
        "ai_keywords": None,
    }


def by_recency(metas):
    """Session metas newest-first by last-updated time."""
    return sorted(metas, key=lambda m: m.get("updated", 0), reverse=True)


def discover_paths():
    """All top-level Claude session JSONLs (excludes subagents/ and nested dirs)."""
    paths = []
    if not os.path.isdir(PROJECTS_DIR):
        return paths
    for entry in os.listdir(PROJECTS_DIR):
        d = os.path.join(PROJECTS_DIR, entry)
        if not os.path.isdir(d):
            continue
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            paths.append(p)
    return paths


def _tool_summary(name, inp):
    if not isinstance(inp, dict):
        return ""
    for k in _TOOL_SUMMARY_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            return v[:160] + ("…" if len(v) > 160 else "")
    return ""


def _user_text(content):
    """Plain text of a user message; None if it's tool results / noise."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if not parts:
            return None
        text = "\n".join(parts)
    else:
        return None
    text = text.strip()
    if not text:
        return None
    for p in _NOISE_PREFIXES:
        if text.startswith(p):
            return None
    return text


def read_chat(path, offset=0):
    """
    Parse chat messages from a Claude transcript, starting at byte `offset`.

    Returns {"messages": [...], "offset": <new>, "truncated": bool}.
    Each message: {"role": "user"|"assistant"|"tool", "text", "ts", "id"}
    (tool messages add "name"). Offset 0 on a huge file reads only the tail.
    """
    msgs, truncated = [], False
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"messages": [], "offset": 0, "truncated": False}
    if offset > size:
        offset = 0
    if offset == 0 and size > TAIL_BYTES + (TAIL_BYTES >> 2):
        offset = size - TAIL_BYTES
        truncated = True

    with open(path, "rb") as f:
        f.seek(offset)
        if truncated:
            f.readline()
        while True:
            pos = f.tell()
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n") and pos + len(raw) == size:
                return {"messages": msgs, "offset": pos, "truncated": truncated}
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if o.get("isMeta") or o.get("isSidechain"):
                continue
            typ = o.get("type")
            m = o.get("message") or {}
            ts = o.get("timestamp")
            uid = o.get("uuid")
            if typ == "user":
                text = _user_text(m.get("content"))
                if text:
                    msgs.append({"role": "user", "text": text[:20000],
                                 "ts": ts, "id": uid})
            elif typ == "assistant":
                content = m.get("content")
                if not isinstance(content, list):
                    continue
                for i, b in enumerate(content):
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        msgs.append({"role": "assistant",
                                     "text": b["text"][:40000],
                                     "ts": ts, "id": "%s.%d" % (uid, i)})
                    elif b.get("type") == "tool_use":
                        msgs.append({"role": "tool",
                                     "name": b.get("name") or "tool",
                                     "text": _tool_summary(b.get("name"),
                                                           b.get("input")),
                                     "ts": ts, "id": "%s.%d" % (uid, i)})
        return {"messages": msgs, "offset": f.tell(), "truncated": truncated}


def transcript_path(meta):
    """Locate the Claude JSONL transcript for a provider meta."""
    p = meta.get("path")
    if p and os.path.exists(p):
        return p
    return None


def _run(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def live_claude_procs():
    """List interactive Claude processes: [{pid, tty, command}]."""
    out = _run(["ps", "-Axo", "pid,tty,comm,command"])
    procs = []
    for ln in out.splitlines()[1:]:
        parts = ln.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, comm, cmd = parts
        if comm == "claude" and tty not in ("??", "?", "-"):
            procs.append({"pid": pid, "tty": tty, "command": cmd})
    return procs


def cwd_of_pid(pid):
    out = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for ln in out.splitlines():
        if ln.startswith("n"):
            return ln[1:]
    return None


def _classify_glyph(title):
    """working | waiting | None, from the leading glyph of a tab title."""
    t = (title or "").strip()
    if not t:
        return None
    c = ord(t[0])
    if 0x2800 <= c <= 0x28FF:
        return "working"
    if t[0] == _IDLE_GLYPH:
        return "waiting"
    return None


def _strip_glyph(title):
    """Drop a leading spinner/sparkle glyph so what's left is the session title."""
    t = (title or "").strip()
    if t and (0x2800 <= ord(t[0]) <= 0x28FF or t[0] == _IDLE_GLYPH):
        t = t[1:].strip()
    return t


def _titles_match(session_title, tab_title):
    """
    True if a session's title and a terminal tab's title refer to the same
    session. The tab title is the session's own aiTitle, but the terminal
    truncates it to the window width, so compare on the common prefix.
    """
    a = (session_title or "").strip().lower()
    b = (tab_title or "").strip().lower()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    if n < 12:
        return a == b
    return a[:n] == b[:n]


_TAB_TITLES_OSA = '''tell application "Terminal"
set out to ""
repeat with w in windows
set tlist to tabs of w
repeat with i from 1 to count of tlist
set t to item i of tlist
try
set ttl to custom title of t
on error
set ttl to ""
end try
if ttl is "" then set ttl to name of t
set out to out & (tty of t) & "::CSM::" & ttl & linefeed
end repeat
end repeat
end tell
return out'''


def terminal_tabs():
    """
    Map tty-basename -> raw tab title for the running Terminal.app.

    Returns {} when Terminal isn't running or when the platform/terminal can't
    be scripted; callers degrade gracefully.
    """
    if "Terminal.app" not in _run(["ps", "-Axo", "comm"]):
        return {}
    out = _run(["osascript", "-e", _TAB_TITLES_OSA], timeout=4)
    tabs = {}
    for ln in out.splitlines():
        if "::CSM::" not in ln:
            continue
        tty, title = ln.split("::CSM::", 1)
        tabs[tty.rsplit("/", 1)[-1]] = title.strip()
    return tabs


def active_sessions(metas):
    """
    Given parsed Claude metas (each with cwd + updated mtime), return
    {session_id: {"pid", "tty", "cwd", "activity", "raw_session_id"}}.
    """
    procs = live_claude_procs()
    if not procs:
        return {}

    for p in procs:
        p["cwd"] = cwd_of_pid(p["pid"])

    tabs = terminal_tabs()
    tty_activity = {tty: _classify_glyph(t) for tty, t in tabs.items()}

    sessions_by_cwd = {}
    for m in metas:
        if m.get("cwd"):
            sessions_by_cwd.setdefault(m["cwd"], []).append(m)
    for lst in sessions_by_cwd.values():
        lst.sort(key=lambda m: m["updated"], reverse=True)

    active = {}
    claimed_sessions = set()

    def _record(meta, proc):
        sid = meta["session_id"]
        active[sid] = {
            "pid": proc["pid"],
            "tty": proc["tty"],
            "cwd": proc["cwd"],
            "activity": tty_activity.get(proc["tty"]),
            "provider": PROVIDER,
            "provider_label": PROVIDER_LABEL,
            "raw_session_id": raw_session_id(meta),
        }
        claimed_sessions.add(sid)

    unmatched = []
    for proc in procs:
        text = _strip_glyph(tabs.get(proc["tty"], ""))
        hits = [m for m in sessions_by_cwd.get(proc["cwd"], [])
                if m["session_id"] not in claimed_sessions
                and _titles_match(m.get("title"), text)] if text else []
        if len(hits) == 1:
            _record(hits[0], proc)
        else:
            unmatched.append(proc)

    by_cwd = {}
    for proc in unmatched:
        if proc["cwd"]:
            by_cwd.setdefault(proc["cwd"], []).append(proc)
    for cwd, plist in by_cwd.items():
        candidates = [m for m in sessions_by_cwd.get(cwd, [])
                      if m["session_id"] not in claimed_sessions]
        for proc, meta in zip(plist, candidates):
            _record(meta, proc)

    return active


def resume_command(meta, dangerously=False, name=None):
    """
    Build the Claude resume command without launching Terminal.

    Returns argv plus the existing shell-command form. `argv` is the canonical
    provider contract; `shell_command` preserves current Terminal resume
    behavior (`cd <cwd> && claude --resume <raw_session_id> ...`).
    """
    raw_id = raw_session_id(meta)
    cwd = meta.get("cwd") if isinstance(meta, dict) else None
    argv = [config.CLAUDE_CLI, "--resume", raw_id]
    if dangerously:
        argv.append("--dangerously-skip-permissions")
    if name:
        argv += ["--name", name]

    parts = []
    if cwd:
        parts.append("cd " + shlex.quote(cwd))
    parts.append(" ".join(shlex.quote(c) for c in argv))

    return {
        "provider": PROVIDER,
        "provider_label": PROVIDER_LABEL,
        "raw_session_id": raw_id,
        "cwd": cwd,
        "argv": argv,
        "shell_command": " && ".join(parts),
    }
