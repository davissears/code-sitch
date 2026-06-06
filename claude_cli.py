"""
claude_cli.py — thin wrapper around the headless `claude -p` CLI.

Used for AI keyword generation and agentic search. `--output-format json`
returns an object whose `result` field holds the model's text answer.
"""

import os
import re
import json
import time
import socket
import shutil
import subprocess

CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")

# ---- connectivity ---------------------------------------------------------
# The session list, liveness, focus and resume are entirely local and keep
# working with no network. Only AI keywords and agentic search need the API, so
# we probe reachability and let those features skip cleanly (and self-heal) when
# offline, instead of spawning `claude -p` calls that would hang and fail.
_net = {"at": 0.0, "ok": True}
_NET_TTL = 10.0


def online(host="api.anthropic.com", port=443, timeout=1.5):
    """Cheap cached check: is the Anthropic API reachable right now?"""
    now = time.time()
    if now - _net["at"] < _NET_TTL:
        return _net["ok"]
    ok = False
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        ok = True
    except OSError:
        ok = False
    _net["at"] = time.time()
    _net["ok"] = ok
    return ok

# fast/cheap for per-session keywords; stronger for ranking relevance
MODEL_KEYWORDS = "claude-haiku-4-5"
MODEL_SEARCH = "claude-sonnet-4-6"


def run_claude(prompt, model=MODEL_KEYWORDS, timeout=90, cwd=None):
    """Run a one-shot headless prompt. Returns the result text, or None on error."""
    if not CLAUDE:
        return None
    # --no-session-persistence: don't write a transcript for our own helper calls,
    # so the monitor never indexes (or recursively enriches) its own AI requests.
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json",
           "--model", model, "--no-session-persistence"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)
    except Exception:
        return None
    if r.returncode != 0 and not r.stdout:
        return None
    try:
        obj = json.loads(r.stdout)
    except Exception:
        return None
    if obj.get("is_error"):
        return None
    return obj.get("result")


def extract_json(text, want="array"):
    """Pull the first JSON array/object out of a model response, tolerating prose."""
    if not text:
        return None
    # fast path
    try:
        return json.loads(text)
    except Exception:
        pass
    open_ch, close_ch = ("[", "]") if want == "array" else ("{", "}")
    start = text.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None
