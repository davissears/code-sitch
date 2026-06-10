"""
bridge.py — two-way chat with a running Claude Code session, remotely.

Read side: a session's .jsonl transcript already records the whole conversation
(user prompts, assistant text, tool calls). We parse it into chat messages and
support cheap incremental reads by byte offset, so a phone can poll for "what's
new" without re-reading megabytes.

Write side: Claude Code is a TUI attached to a Terminal.app tab. Terminal's
AppleScript `do script ... in tab` types text into whatever is running in that
tab — including a raw-mode TUI — so we can deliver a message to the *same*
session you see on screen, keyed by its tty. Claude Code treats a multi-line
burst as a paste (it inserts the newlines without submitting), so we send the
text first and a bare Return a beat later to submit. Verified end to end on
macOS 26 / Terminal.app / Claude Code 2.x.

The injected message appears in the terminal scrollback and the transcript like
any typed prompt, so picking the conversation back up at the laptop is seamless.
"""

import json
import os
import subprocess

MAX_SEND_CHARS = 16000          # keep injections sane; CC handles big pastes fine
TAIL_BYTES = 1 << 20            # first load of a huge transcript: last ~1 MB only


# --------------------------------------------------------------------------- #
# read side — transcript -> chat messages
# --------------------------------------------------------------------------- #
_NOISE_PREFIXES = (
    "<command-name>", "<local-command", "<command-message>",
    "<system-reminder>", "Caveat:", "[Request interrupted",
)

# tool_use input keys worth showing in a one-line chip, in preference order
_TOOL_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url",
                      "query", "description", "prompt", "skill", "subject")


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
            return None                      # tool_result-only turn
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
    Parse chat messages from a transcript, starting at byte `offset`.

    Returns {"messages": [...], "offset": <new>, "truncated": bool}.
    Each message: {"role": "user"|"assistant"|"tool", "text", "ts", "id"}
    (tool messages add "name"). Offset 0 on a huge file reads only the tail.
    """
    msgs, truncated = [], False
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"messages": [], "offset": 0, "truncated": False}
    if offset > size:                        # transcript rotated/shrunk — restart
        offset = 0
    if offset == 0 and size > TAIL_BYTES + (TAIL_BYTES >> 2):
        offset = size - TAIL_BYTES
        truncated = True

    with open(path, "rb") as f:
        f.seek(offset)
        if truncated:
            f.readline()                     # skip the partial line we landed in
        while True:
            pos = f.tell()
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n") and pos + len(raw) == size:
                # a writer is mid-line; leave it for the next poll
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
    """Locate the .jsonl for a session meta (sessions.py keeps the path)."""
    p = meta.get("path")
    if p and os.path.exists(p):
        return p
    return None


# --------------------------------------------------------------------------- #
# write side — inject a message into the session's Terminal tab by tty
# --------------------------------------------------------------------------- #
# Two AppleScript gotchas, both verified the hard way:
#   • `do script ... in t` where t is a repeat-loop variable silently no-ops;
#     the tab must be addressed explicitly as `tab j of window id N`.
#   • text and tty arrive via argv (`on run argv`), so no string escaping —
#     newlines and quotes pass through verbatim.
_SEND_OSA = '''on run argv
  set theTTY to item 1 of argv
  set theText to item 2 of argv
  tell application "Terminal"
    repeat with w in windows
      set wid to id of w
      repeat with j from 1 to count of tabs of window id wid
        if (tty of tab j of window id wid) is theTTY then
          do script theText in tab j of window id wid
          delay 0.5
          do script "" in tab j of window id wid
          return "ok"
        end if
      end repeat
    end repeat
    return "no tab with tty " & theTTY
  end tell
end run'''


def send_to_session(tty, text):
    """
    Type `text` into the Claude Code session on `tty` and submit it.
    Returns {ok, detail}. The message lands in the live TUI exactly as if
    typed there, so the terminal and the transcript both show it.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "detail": "empty message"}
    if len(text) > MAX_SEND_CHARS:
        return {"ok": False, "detail": "message too long (%d > %d chars)"
                % (len(text), MAX_SEND_CHARS)}
    ttypath = tty if tty.startswith("/dev/") else "/dev/" + tty
    try:
        r = subprocess.run(["osascript", "-e", _SEND_OSA, ttypath, text],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return {"ok": False, "detail": "osascript failed: %s" % e}
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return {"ok": False, "detail": (r.stderr or "osascript error").strip()}
    if out != "ok":
        return {"ok": False, "detail": out or "tab not found"}
    return {"ok": True, "detail": "delivered to %s" % ttypath}
