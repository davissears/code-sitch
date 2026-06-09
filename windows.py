"""
windows.py — focus a running session's window, or resume a dead one.

Focus (active session): we know the controlling tty of the live claude process
(from liveness). Terminal.app exposes each tab's tty via AppleScript, so we find
the matching tab, select it, and raise its window. For correct behaviour when
the window lives on another Mission Control space we hand the final focus to
yabai (which can cross spaces without the scripting addition).

Resume (inactive session): open a brand-new Terminal window, cd into the
session's original cwd, and run `claude --resume <id>` so the conversation comes
back exactly where it left off.

Currently targets Apple Terminal (the terminal in use on this machine).
"""

import json
import shlex
import subprocess


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _osascript(script, timeout=10):
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def _as_str(s):
    """Escape a Python string for embedding in an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _yabai_focus_terminal(keys):
    """
    Best-effort: focus the Terminal yabai window (handles space switching).

    `keys` is a list of stable substrings to look for in a window's title, most
    specific first (e.g. the session id, then the session title). We match on
    these rather than the whole title because Terminal titles are volatile — the
    leading spinner glyph animates and the trailing "▸ <process>" segment changes
    as Claude spawns subprocesses, so an exact-title compare races and misses.
    """
    keys = [k for k in keys if k]
    try:
        out = subprocess.run(["yabai", "-m", "query", "--windows"],
                             capture_output=True, text=True, timeout=4).stdout
        wins = json.loads(out)
    except Exception:
        return False
    terms = [w for w in wins if w.get("app") in ("Terminal", "iTerm2")]
    target = None
    for key in keys:                 # try most-specific key first
        hits = [w for w in terms if key in (w.get("title") or "")]
        if len(hits) == 1:
            target = hits[0]
            break
    if target is None and len(terms) == 1:
        target = terms[0]            # unambiguous: only one terminal window
    if target is None:
        return False
    try:
        subprocess.run(["yabai", "-m", "window", "--focus", str(target["id"])],
                       capture_output=True, text=True, timeout=4)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# focus an active session by its tty
# --------------------------------------------------------------------------- #
def focus_session(info):
    """info: {tty, pid, cwd, session_id?, title?}. Returns {ok, method, detail}."""
    tty = info.get("tty")
    if not tty:
        return {"ok": False, "method": None, "detail": "no tty for session"}
    ttypath = tty if tty.startswith("/dev/") else "/dev/" + tty

    script = '''
    tell application "Terminal"
      set theName to ""
      repeat with w in windows
        repeat with t in tabs of w
          if (tty of t) is "%s" then
            set selected of t to true
            set frontmost of w to true
            set index of w to 1
            set theName to (name of w)
            activate
            return theName
          end if
        end repeat
      end repeat
      return ""
    end tell
    ''' % ttypath

    code, out, err = _osascript(script)
    if code != 0:
        return {"ok": False, "method": "applescript", "detail": err or "osascript failed"}
    if out == "":
        return {"ok": False, "method": "applescript",
                "detail": "no Terminal tab found for %s" % ttypath}

    # hand off to yabai so we land on the right Space, too. Match on stable
    # tokens (session id, then title) rather than `out` — the live window title
    # animates and would race; `out` is only the last-resort key.
    yb = _yabai_focus_terminal([info.get("session_id"), info.get("title"), out])
    return {"ok": True,
            "method": "applescript+yabai" if yb else "applescript",
            "detail": "focused %s" % out}


# --------------------------------------------------------------------------- #
# resume an inactive session in a fresh window
# --------------------------------------------------------------------------- #
def resume_session(meta, dangerously=False, name=None):
    """Open a new Terminal window resuming `meta`. Returns {ok, command, detail}."""
    sid = meta["session_id"]
    cwd = meta.get("cwd")

    parts = []
    if cwd:
        parts.append("cd " + shlex.quote(cwd))
    cmd = ["claude", "--resume", sid]
    if dangerously:
        cmd.append("--dangerously-skip-permissions")
    if name:
        cmd += ["--name", name]
    parts.append(" ".join(shlex.quote(c) for c in cmd))
    shell_cmd = " && ".join(parts)

    script = '''
    tell application "Terminal"
      activate
      do script "%s"
    end tell
    ''' % _as_str(shell_cmd)

    code, out, err = _osascript(script)
    if code != 0:
        return {"ok": False, "command": shell_cmd, "detail": err or "osascript failed"}
    return {"ok": True, "command": shell_cmd, "detail": "opened new Terminal window"}


if __name__ == "__main__":
    # smoke test: focus the current session's own window via its tty
    import sys
    import sessions
    import liveness
    idx = sessions.SessionIndex(); idx.reindex()
    act = liveness.active_sessions(idx.all())
    if not act:
        print("no active sessions to test focus on")
        sys.exit(0)
    sid, info = next(iter(act.items()))
    print("focusing:", idx.get(sid)["title"], "->", focus_session(info))
