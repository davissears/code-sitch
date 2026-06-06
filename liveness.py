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

    active = {}
    for cwd, plist in by_cwd.items():
        candidates = sessions_by_cwd.get(cwd, [])
        # assign newest sessions to the live procs in this cwd
        for proc, meta in zip(plist, candidates):
            active[meta["session_id"]] = {
                "pid": proc["pid"],
                "tty": proc["tty"],
                "cwd": cwd,
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
