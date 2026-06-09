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
    Given parsed session metas (each with cwd + updated mtime), return
    {session_id: {"pid", "tty", "cwd", "activity"}} for sessions with a live
    process.

    Two signals connect a live process to a session:
      • the terminal tab title — it carries the session's own aiTitle, so it
        names the session exactly (used first; survives shared directories), and
      • the working directory — a fallback when the title can't be read (a
        non-Terminal terminal) or doesn't match.

    Tab titles are the reliable signal: when several sessions share one cwd,
    cwd+mtime alone can't tell which terminal runs which session, so "focus"
    could land on the wrong window. Matching on the title fixes that.
    """
    procs = live_claude_procs()
    if not procs:
        return {}

    for p in procs:
        p["cwd"] = cwd_of_pid(p["pid"])

    tabs = terminal_tabs()                       # tty -> raw title
    tty_activity = {tty: _classify_glyph(t) for tty, t in tabs.items()}

    # sessions per cwd, newest first (for the cwd fallback)
    sessions_by_cwd = {}
    for m in metas:
        if m.get("cwd"):
            sessions_by_cwd.setdefault(m["cwd"], []).append(m)
    for lst in sessions_by_cwd.values():
        lst.sort(key=lambda m: m["updated"], reverse=True)

    active = {}
    claimed_sessions = set()

    def _record(meta, proc):
        active[meta["session_id"]] = {
            "pid": proc["pid"],
            "tty": proc["tty"],
            "cwd": proc["cwd"],
            # working | waiting | None(unknown, e.g. non-Terminal terminal)
            "activity": tty_activity.get(proc["tty"]),
        }
        claimed_sessions.add(meta["session_id"])

    # Pass 1 — match each process to the session named in its terminal tab title.
    # Restrict candidates to the proc's own cwd so two same-named sessions in
    # different directories can't cross-match.
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

    # Pass 2 — fallback for processes with no readable/unique title match:
    # assign the newest still-unclaimed session in the same cwd (best effort).
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
