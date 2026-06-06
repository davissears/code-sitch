#!/usr/bin/env python3
"""
server.py — local web server for the Claude Code Situation Monitor.

Serves a single-page UI plus a small JSON API:

  GET  /                 the UI
  GET  /api/sessions     all sessions + live status (sorted newest first)
  POST /api/focus        {session_id}            -> focus the running window
  POST /api/resume       {session_id, ...}       -> open a new resumed window
  POST /api/search       {query}                 -> agentic relevance search
  POST /api/refresh      force a reindex

Runs entirely on localhost. No third-party dependencies (Python stdlib only).
Background threads keep the index fresh and enrich sessions with AI keywords.
"""

import os
import sys
import json
import time
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import sessions
import liveness
import windows
import keywords as kw
import agentic
import claude_cli

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("CSM_PORT", "8787"))
HOST = os.environ.get("CSM_HOST", "127.0.0.1")
ENRICH_LIMIT = int(os.environ.get("CSM_ENRICH_LIMIT", "50"))

INDEX = sessions.SessionIndex()
ENRICHER = kw.KeywordEnricher(INDEX, limit=ENRICH_LIMIT)

# liveness is mildly expensive (ps + lsof); cache it briefly
_live_lock = threading.Lock()
_live_cache = {"at": 0.0, "data": {}}
LIVE_TTL = 2.5


def live_map():
    now = time.time()
    with _live_lock:
        if now - _live_cache["at"] < LIVE_TTL:
            return _live_cache["data"]
    data = liveness.active_sessions(INDEX.all())
    with _live_lock:
        _live_cache["at"] = time.time()
        _live_cache["data"] = data
    return data


def session_view(meta, active):
    info = active.get(meta["session_id"])
    # heuristic keywords are a pure function of the static text, so compute them
    # once per parse and reuse across polls (a fresh reparse drops the field)
    hk = meta.get("heuristic_keywords")
    if hk is None:
        hk = kw.heuristic_keywords(meta, limit=20)
        meta["heuristic_keywords"] = hk
    pool = list(dict.fromkeys((meta.get("ai_keywords") or []) + hk))[:30]
    # one lowercase blob the browser can substring-search instantly
    haystack = " ".join(filter(None, [
        meta.get("title", ""), meta.get("project", ""),
        meta.get("git_branch", "") or "", meta.get("slug", "") or "",
        (meta.get("first_prompt") or "")[:400],
        (meta.get("last_prompt") or "")[:300],
        " ".join(pool),
    ])).lower()[:1800]
    return {
        "id": meta["session_id"],
        "title": meta.get("title"),
        "project": meta.get("project"),
        "cwd": meta.get("cwd"),
        "branch": meta.get("git_branch"),
        "updated": meta.get("updated"),
        "messages": meta.get("messages", 0),
        "active": info is not None,
        "first_prompt": (meta.get("first_prompt") or "")[:280],
        "last_prompt": (meta.get("last_prompt") or "")[:200],
        "keywords": pool,
        "haystack": haystack,
    }


PERIODS = [("today", "Today"), ("week", "Last 7 days"),
           ("month", "Last 30 days"), ("all", "All time")]


def usage_stats(metas):
    """One row per period: chats active, messages you sent, and tokens used."""
    today = date.today()
    # rolling windows (not calendar): "last 7 / 30 days, including today". Calendar
    # weeks/months collide at the start of a month and read as a bug; rolling
    # windows are what "usage this week/month" actually means and stay monotonic.
    week_start = today - timedelta(days=6)
    month_start = today - timedelta(days=29)

    def periods_of(d):
        """Which period keys a given local date falls into."""
        ks = ["all"]
        if d >= month_start:
            ks.append("month")
        if d >= week_start:
            ks.append("week")
        if d == today:
            ks.append("today")
        return ks

    chats = {k: 0 for k, _ in PERIODS}
    msgs = {k: 0 for k, _ in PERIODS}
    toks = {k: [0, 0, 0, 0] for k, _ in PERIODS}   # input, output, cache_create, cache_read

    for m in metas:
        ubd = m.get("user_by_day") or {}
        tbd = m.get("usage_by_day") or {}
        # parse each distinct day once, then reuse its period set for all metrics
        day_periods = {}
        for day_str in set(ubd) | set(tbd):
            try:
                day_periods[day_str] = periods_of(date.fromisoformat(day_str))
            except Exception:
                pass
        for k in set().union(*day_periods.values()):   # periods this session was active in
            chats[k] += 1
        for day_str, c in ubd.items():
            for k in day_periods.get(day_str, ()):
                msgs[k] += c
        for day_str, vals in tbd.items():
            for k in day_periods.get(day_str, ()):
                for i in range(4):
                    toks[k][i] += vals[i]

    # all-time = every chat that exists (incl. empty/aborted ones with no datable
    # activity), so it matches the total session count shown in the header
    chats["all"] = len(metas)

    rows = []
    for key, label in PERIODS:
        t = toks[key]
        rows.append({
            "key": key, "label": label,
            "chats": chats[key], "messages": msgs[key],
            # headline = "new" tokens (input + cache writes + output); cache reads
            # are cheap context replays, surfaced separately in the tooltip
            "tokens": {"input": t[0], "output": t[1], "cache_creation": t[2],
                       "cache_read": t[3], "headline": t[0] + t[2] + t[1]},
        })
    return {"rows": rows}


def sessions_payload():
    active = live_map()
    metas = sessions.by_recency(INDEX.all())
    views = [session_view(m, active) for m in metas]
    return {
        "sessions": views,
        "active_count": sum(1 for v in views if v["active"]),
        "total": len(views),
        "indexing": INDEX.indexing,
        "progress": INDEX.progress,
        "enrich": ENRICHER.status,
        "online": claude_cli.online(),   # AI features (keywords, agentic search) reachable?
        "stats": usage_stats(metas),
        "generated_at": time.time(),
    }


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "CSM/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[csm] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _static(self, fname, ctype):
        path = os.path.join(STATIC, fname)
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, {"error": "not found"})

    # ---- routing ----
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/" or p == "/index.html":
            return self._static("index.html", "text/html; charset=utf-8")
        if p == "/app.js":
            return self._static("app.js", "application/javascript; charset=utf-8")
        if p == "/style.css":
            return self._static("style.css", "text/css; charset=utf-8")
        if p == "/favicon.svg":
            return self._static("favicon.svg", "image/svg+xml")
        if p == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if p == "/api/sessions":
            return self._send(200, sessions_payload())
        if p == "/api/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        data = self._body()

        if p == "/api/focus":
            sid = data.get("session_id")
            active = live_map()
            info = active.get(sid)
            if not info:
                return self._send(409, {"ok": False, "detail": "session is no longer active"})
            return self._send(200, windows.focus_session(info))

        if p == "/api/resume":
            sid = data.get("session_id")
            meta = INDEX.get(sid)
            if not meta:
                return self._send(404, {"ok": False, "detail": "unknown session"})
            res = windows.resume_session(
                meta,
                dangerously=bool(data.get("dangerously")),
                name=data.get("name"),
            )
            # the new window won't show as active until it writes to its log;
            # invalidate liveness so the next poll re-checks promptly
            with _live_lock:
                _live_cache["at"] = 0.0
            return self._send(200, res)

        if p == "/api/search":
            query = (data.get("query") or "").strip()
            if not query:
                return self._send(400, {"error": "empty query"})
            res = agentic.agentic_search(query, INDEX.all())
            return self._send(200, res)

        if p == "/api/refresh":
            threading.Thread(target=_refresh_now, daemon=True).start()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


# --------------------------------------------------------------------------- #
# background maintenance
# --------------------------------------------------------------------------- #
def _refresh_now():
    INDEX.reindex()
    with _live_lock:
        _live_cache["at"] = 0.0
    if not ENRICHER.status["running"]:
        ENRICHER.start()


def maintenance_loop():
    while True:
        try:
            INDEX.reindex()
            if not ENRICHER.status["running"]:
                # start/continue AI keyword enrichment for any new sessions
                ENRICHER.start()
        except Exception as e:
            sys.stderr.write("[csm] maintenance error: %s\n" % e)
        time.sleep(8)


def main():
    sys.stderr.write("[csm] indexing sessions...\n")
    INDEX.reindex()
    sys.stderr.write("[csm] %d sessions indexed\n" % len(INDEX.all()))
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        # most likely the port is already serving another instance — exit cleanly
        # (exit 0 so a restart-on-crash LaunchAgent doesn't loop)
        sys.stderr.write("[csm] could not bind %s:%d (%s) — already running?\n"
                         % (HOST, PORT, e))
        sys.exit(0)

    threading.Thread(target=maintenance_loop, daemon=True).start()
    ENRICHER.start()
    url = "http://%s:%d/" % (HOST, PORT)
    sys.stderr.write("[csm] serving on %s\n" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[csm] shutting down\n")
        httpd.shutdown()


if __name__ == "__main__":
    main()
