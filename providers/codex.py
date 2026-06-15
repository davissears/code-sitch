"""
Codex provider adapter.

Codex stores thread metadata in ~/.codex/sqlite/state_5.sqlite and rollout
JSONL transcripts separately. This module keeps that storage format behind the
same lightweight meta/chat/resume contract used by the Claude provider.
"""

import json
import os
import shlex
import sqlite3
from datetime import datetime, timezone
from urllib.parse import quote

import config

PROVIDER = "codex"
PROVIDER_LABEL = "Codex"
CAPABILITIES = {
    "chat": True,
    "resume": True,
    "live": False,
    "send": False,
    "focus": False,
}

DB_PATH = config.CODEX_STATE_DB
TAIL_BYTES = 1 << 20

THREAD_COLUMNS = (
    "id", "rollout_path", "created_at", "updated_at", "created_at_ms",
    "updated_at_ms", "source", "thread_source", "model_provider", "cwd",
    "title", "tokens_used", "has_user_event", "archived", "git_branch",
    "first_user_message", "model", "reasoning_effort", "preview",
)

_TOOL_SUMMARY_KEYS = (
    "command", "cmd", "file_path", "path", "pattern", "url", "query",
    "description", "prompt", "skill", "subject",
)


def qualify_session_id(raw_session_id):
    raw = raw_session_id or ""
    return raw if raw.startswith(PROVIDER + ":") else PROVIDER + ":" + raw


def capabilities():
    return dict(CAPABILITIES)


def raw_session_id(meta_or_id):
    if isinstance(meta_or_id, dict):
        sid = (
            meta_or_id.get("raw_session_id")
            or meta_or_id.get("thread_id")
            or meta_or_id.get("session_id")
            or meta_or_id.get("id")
            or ""
        )
    else:
        sid = meta_or_id or ""
    prefix = PROVIDER + ":"
    return sid[len(prefix):] if sid.startswith(prefix) else sid


def _iso_to_local_day(s):
    if not s:
        return None
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return None


def _project_label(cwd):
    if cwd:
        parts = [p for p in cwd.split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        if parts:
            return parts[-1]
    return "(unknown)"


def _epoch_seconds(row, ms_key, sec_key):
    value = row.get(ms_key)
    if value:
        try:
            return int(value) / 1000.0
        except (TypeError, ValueError):
            pass
    value = row.get(sec_key)
    if value:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _compact(text, limit):
    text = " ".join((text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _payload_text(payload):
    text = payload.get("message")
    if isinstance(text, str):
        return text.strip()
    return ""


def _tool_summary(name, arguments):
    if not arguments:
        return ""
    obj = None
    if isinstance(arguments, dict):
        obj = arguments
    elif isinstance(arguments, str):
        try:
            obj = json.loads(arguments)
        except Exception:
            return _compact(arguments, 160)
    if isinstance(obj, dict):
        for key in _TOOL_SUMMARY_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return _compact(value, 160)
    return ""


def _add_token_usage(bucket, usage):
    if not isinstance(usage, dict):
        return
    input_tokens = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    bucket[0] += max(input_tokens - cached, 0)
    bucket[1] += output
    bucket[3] += cached


def _row_dict(row):
    if isinstance(row, dict):
        return row
    return {k: row[k] for k in row.keys()}


def _connect(db_path=DB_PATH):
    if not os.path.exists(db_path):
        return None
    uri = "file:%s?mode=ro" % quote(db_path)
    conn = sqlite3.connect(uri, uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def discover_threads(db_path=DB_PATH):
    """Return Codex thread rows as plain dicts, newest first."""
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        cols = ", ".join(THREAD_COLUMNS)
        rows = conn.execute(
            "select %s from threads order by coalesce(updated_at_ms, updated_at * 1000) desc, id desc" % cols
        ).fetchall()
        return [_row_dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def cache_id(row):
    return PROVIDER + ":" + raw_session_id(row)


def cache_signature(row):
    path = row.get("rollout_path")
    mtime = size = 0
    if path:
        try:
            st = os.stat(path)
            mtime = int(st.st_mtime)
            size = st.st_size
        except OSError:
            pass
    return "%s:%s:%s:%s" % (row.get("updated_at_ms") or row.get("updated_at") or "", row.get("title") or "", mtime, size)


def parse_session(row):
    """Parse one Codex thread row plus its rollout JSONL into a provider meta."""
    row = _row_dict(row)
    thread_id = row.get("id")
    if not thread_id:
        return None

    path = row.get("rollout_path")
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None

    first_prompt = (row.get("first_user_message") or "").strip() or None
    last_prompt = None
    samples = []
    n_user = 0
    n_assistant = 0
    usage_by_day = {}
    user_by_day = {}

    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                for raw in f:
                    if b'"type":"event_msg"' not in raw and not (
                        b'"type":"response_item"' in raw and b'"function_call"' in raw
                    ):
                        continue
                    try:
                        o = json.loads(raw)
                    except Exception:
                        continue
                    typ = o.get("type")
                    payload = o.get("payload") or {}
                    ts = o.get("timestamp")
                    if typ == "event_msg":
                        ptype = payload.get("type")
                        if ptype == "user_message":
                            text = _payload_text(payload)
                            if text:
                                n_user += 1
                                day = _iso_to_local_day(ts)
                                if day:
                                    user_by_day[day] = user_by_day.get(day, 0) + 1
                                if first_prompt is None:
                                    first_prompt = text
                                last_prompt = text
                                if len(samples) < 6:
                                    samples.append(text[:400])
                        elif ptype == "agent_message":
                            if _payload_text(payload):
                                n_assistant += 1
                        elif ptype == "token_count":
                            day = _iso_to_local_day(ts)
                            usage = ((payload.get("info") or {}).get("last_token_usage") or {})
                            if day and usage:
                                bucket = usage_by_day.get(day)
                                if bucket is None:
                                    bucket = usage_by_day[day] = [0, 0, 0, 0]
                                _add_token_usage(bucket, usage)
                    elif typ == "response_item" and payload.get("type") == "function_call":
                        n_assistant += 1
        except OSError:
            pass

    if first_prompt and not samples:
        samples.append(first_prompt[:400])

    title = (row.get("title") or "").strip()
    if not title:
        title = (first_prompt[:80] if first_prompt else None) or (row.get("preview") or "").strip() or "(untitled session)"

    updated = _epoch_seconds(row, "updated_at_ms", "updated_at")
    if updated is None and st:
        updated = st.st_mtime

    return {
        "provider": PROVIDER,
        "provider_label": PROVIDER_LABEL,
        "capabilities": capabilities(),
        "session_id": qualify_session_id(thread_id),
        "raw_session_id": thread_id,
        "thread_id": thread_id,
        "path": path,
        "project_dir": None,
        "project": _project_label(row.get("cwd")),
        "cwd": row.get("cwd"),
        "git_branch": row.get("git_branch"),
        "slug": None,
        "title": title,
        "ai_title": title,
        "first_prompt": first_prompt,
        "last_prompt": last_prompt,
        "samples": samples,
        "messages": n_assistant,
        "user_turns": n_user,
        "created": _epoch_seconds(row, "created_at_ms", "created_at"),
        "updated": updated or 0,
        "size": st.st_size if st else 0,
        "usage_by_day": usage_by_day,
        "user_by_day": user_by_day,
        "archived": bool(row.get("archived")),
        "model": row.get("model"),
        "reasoning_effort": row.get("reasoning_effort"),
        "ai_keywords": None,
    }


def read_chat(path, offset=0):
    """Parse visible chat messages from a Codex rollout JSONL."""
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
            if b'"type":"event_msg"' not in raw and not (
                b'"type":"response_item"' in raw and b'"function_call"' in raw
            ):
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            typ = o.get("type")
            payload = o.get("payload") or {}
            ts = o.get("timestamp")
            if typ == "event_msg":
                ptype = payload.get("type")
                if ptype == "user_message":
                    text = _payload_text(payload)
                    if text:
                        msgs.append({
                            "role": "user", "text": text[:20000], "ts": ts,
                            "id": payload.get("client_id") or "user.%d" % pos,
                        })
                elif ptype == "agent_message":
                    text = _payload_text(payload)
                    if text:
                        msgs.append({
                            "role": "assistant", "text": text[:40000], "ts": ts,
                            "id": "agent.%d" % pos,
                        })
            elif typ == "response_item" and payload.get("type") == "function_call":
                name = payload.get("name") or "tool"
                msgs.append({
                    "role": "tool",
                    "name": name,
                    "text": _tool_summary(name, payload.get("arguments")),
                    "ts": ts,
                    "id": payload.get("call_id") or "tool.%d" % pos,
                })
        return {"messages": msgs, "offset": f.tell(), "truncated": truncated}


def transcript_path(meta):
    p = meta.get("path")
    if p and os.path.exists(p):
        return p
    return None


def active_sessions(metas):
    """Codex Desktop/CLI liveness is not mapped to Terminal tabs yet."""
    return {}


def resume_command(meta, dangerously=False, name=None):
    """Build the Codex resume command without launching Terminal."""
    raw_id = raw_session_id(meta)
    cwd = meta.get("cwd") if isinstance(meta, dict) else None
    argv = [config.CODEX_CLI, "resume", raw_id]

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
