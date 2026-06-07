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


def terminal_tab_activity():
    """
    Map tty-basename -> "working"|"waiting" by reading Terminal.app tab titles.

    Returns {} when Terminal isn't running (we never launch it just to ask) or
    when the platform/terminal can't be scripted — callers degrade to a plain
    "active" badge in that case.
    """
    # only ask if Terminal.app is already running — never launch it just to peek
    if "Terminal.app" not in _run(["ps", "-Axo", "comm"]):
        return {}
    out = _run(["osascript", "-e", _TAB_TITLES_OSA], timeout=4)
    activity = {}
    for ln in out.splitlines():
        if "::CSM::" not in ln:
            continue
        tty, title = ln.split("::CSM::", 1)
        st = _classify_glyph(title)
        if st:
            activity[tty.rsplit("/", 1)[-1]] = st   # /dev/ttys006 -> ttys006
    return activity


def active_sessions(metas):
    """
    Given parsed session metas (each with cwd + updated mtime), return
    {session_id: {"pid", "tty", "cwd"}} for sessions that have a live process.
    """
    procs = live_claude_procs()
    if not procs:
        return {}

    # group live processes by their resolved cwd
    by_cwd = {}
    for p in procs:
        cwd = cwd_of_pid(p["pid"])
        if not cwd:
            continue
        by_cwd.setdefault(cwd, []).append(p)

    # sessions per cwd, newest first
    sessions_by_cwd = {}
    for m in metas:
        if m.get("cwd"):
            sessions_by_cwd.setdefault(m["cwd"], []).append(m)
    for lst in sessions_by_cwd.values():
        lst.sort(key=lambda m: m["updated"], reverse=True)

    tty_activity = terminal_tab_activity()   # tty -> working|waiting (best effort)

    active = {}
    for cwd, plist in by_cwd.items():
        candidates = sessions_by_cwd.get(cwd, [])
        # assign newest sessions to the live procs in this cwd
        for proc, meta in zip(plist, candidates):
            active[meta["session_id"]] = {
                "pid": proc["pid"],
                "tty": proc["tty"],
                "cwd": cwd,
                # working | waiting | None(unknown, e.g. non-Terminal terminal)
                "activity": tty_activity.get(proc["tty"]),
            }
    return active


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
