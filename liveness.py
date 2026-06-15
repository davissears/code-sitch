"""
liveness.py — figure out which sessions are *currently running*.

A `claude` process does not keep its session .jsonl open, so we can't lsof our
way to the session id. But we can get each live claude process's working
directory and controlling tty, and every session records its cwd. So:

    live claude proc  --(cwd)-->  the session in that cwd with the newest mtime
                      --(tty)-->  the Terminal tab/window to focus

This is validated against the real machine. If two claude procs share a cwd we
assign the N newest sessions in that cwd to them (best effort).

Returns a map: session_id -> {"pid", "tty", "cwd"}.
"""

import subprocess

from providers import claude


def _run(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def live_claude_procs():
    """List interactive claude processes: [{pid, tty, command}]."""
    out = _run(["ps", "-Axo", "pid,tty,comm,command"])
    procs = []
    for ln in out.splitlines()[1:]:
        parts = ln.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, comm, cmd = parts
        # the launcher binary reports comm == "claude"; require a real tty so we
        # skip headless `claude -p` helpers (those have tty "??").
        if comm == "claude" and tty not in ("??", "?", "-"):
            procs.append({"pid": pid, "tty": tty, "command": cmd})
    return procs


def cwd_of_pid(pid):
    out = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for ln in out.splitlines():
        if ln.startswith("n"):
            return ln[1:]
    return None


# --------------------------------------------------------------------------- #
# working vs. waiting — read it straight off the terminal tab title.
#
# Claude Code animates the tab/window title while it is generating: the title is
# prefixed with a Braille spinner glyph (U+2800–U+28FF, the dots cycle). When it
# finishes and is waiting for your next prompt, the prefix is a steady ✳ sparkle.
# So the leading glyph of the tab title IS Claude's own "am I busy" state — we
# just read it back. We key this by tty (which `ps` reports authoritatively per
# process), so it stays correct even when several sessions share one cwd and the
# session→pid assignment above is only best-effort.
# --------------------------------------------------------------------------- #
_IDLE_GLYPH = "✳"          # ✳ — Claude is waiting for your input


def _classify_glyph(title):
    """working | waiting | None, from the leading glyph of a tab title."""
    t = (title or "").strip()
    if not t:
        return None
    c = ord(t[0])
    if 0x2800 <= c <= 0x28FF:    # Braille spinner → actively generating
        return "working"
    if t[0] == _IDLE_GLYPH:      # steady sparkle → awaiting your prompt
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
    truncates it to the window width — so we compare on the common prefix.
    """
    a = (session_title or "").strip().lower()
    b = (tab_title or "").strip().lower()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    if n < 12:                  # too short to trust — demand an exact match
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

    Returns {} when Terminal isn't running (we never launch it just to ask) or
    when the platform/terminal can't be scripted — callers degrade gracefully.
    """
    # only ask if Terminal.app is already running — never launch it just to peek
    if "Terminal.app" not in _run(["ps", "-Axo", "comm"]):
        return {}
    out = _run(["osascript", "-e", _TAB_TITLES_OSA], timeout=4)
    tabs = {}
    for ln in out.splitlines():
        if "::CSM::" not in ln:
            continue
        tty, title = ln.split("::CSM::", 1)
        tabs[tty.rsplit("/", 1)[-1]] = title.strip()   # /dev/ttys006 -> ttys006
    return tabs


def active_sessions(metas):
    """
    Compatibility wrapper for Claude Terminal liveness.

    Provider-neutral liveness aggregation lives in sessions.active_sessions().
    """
    return claude.active_sessions(metas)


if __name__ == "__main__":
    import sessions
    idx = sessions.SessionIndex()
    idx.reindex()
    metas = idx.all()
    print("live claude procs:", live_claude_procs())
    act = active_sessions(metas)
    print("\nactive sessions (%d):" % len(act))
    for sid, info in act.items():
        m = idx.get(sid)
        print("  •", (m["title"] or "")[:50], "| tty", info["tty"], "| pid", info["pid"])
