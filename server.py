#!/usr/bin/env python3
"""
server.py — local web server for the Claude Code Situation Monitor.

Serves a single-page UI plus a small JSON API:

  GET  /                 the UI
  GET  /chat?session=ID  remote chat with one session (phone-friendly)
  GET  /api/sessions     all sessions + live status (sorted newest first)
  GET  /api/chat         ?session_id&offset      -> chat messages + status
  POST /api/chat/send    {session_id, text}      -> type into the live session
  POST /api/login        {token}                 -> auth cookie (remote mode)
  POST /api/focus        {session_id}            -> focus the running window
  POST /api/resume       {session_id, ...}       -> open a new resumed window
  POST /api/search       {query}                 -> agentic relevance search
  POST /api/refresh      force a reindex

Defaults to localhost with no auth. For remote access (e.g. over Tailscale),
put a secret in CSM_STATE_DIR/token (or CSM_TOKEN) and every request must
present it — see REMOTE.md. Python stdlib only.
"""

import hmac
import os
import sys
import json
import time
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import sessions
import windows
import bridge
import keywords as kw
import agentic
import claude_cli
import config

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
PORT = int(os.environ.get("CSM_PORT", "8787"))
HOST = os.environ.get("CSM_HOST", "127.0.0.1")
ENRICH_LIMIT = int(os.environ.get("CSM_ENRICH_LIMIT", "50"))

TOKEN_FILE = os.path.join(config.STATE_DIR, "token")


def _load_token():
    """Remote-access secret: CSM_TOKEN env, else the token file. None = no auth."""
    t = os.environ.get("CSM_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        return t or None
    except OSError:
        return None


TOKEN = _load_token()

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
    data = sessions.active_sessions(INDEX.all())
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
        meta.get("provider_label") or meta.get("provider") or "",
    ])).lower()[:1800]
    return {
        "id": meta["session_id"],
        "raw_id": meta.get("raw_session_id"),
        "provider": meta.get("provider") or "claude",
        "provider_label": meta.get("provider_label") or "Claude",
        "capabilities": sessions.capabilities_for_meta(meta),
        "title": meta.get("title"),
        "project": meta.get("project"),
        "cwd": meta.get("cwd"),
        "branch": meta.get("git_branch"),
        "updated": meta.get("updated"),
        "messages": meta.get("messages", 0),
        "active": info is not None,
        # "working" (generating) | "waiting" (awaiting your prompt) | None (running,
        # but we couldn't read the terminal title). Only meaningful when active.
        "activity": info.get("activity") if info else None,
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


def session_capability(meta, name):
    return bool(sessions.capabilities_for_meta(meta).get(name))


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "CSM/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[csm] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    # ---- auth (only when a token is configured; localhost default = open) ----
    def _presented_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "csm_token":
                return v
        return ""

    def _authed(self):
        if not TOKEN:
            return True
        return hmac.compare_digest(self._presented_token(), TOKEN)

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
        parsed = urlparse(self.path)
        p = parsed.path
        # unauthenticated essentials: favicon + health probe
        if p == "/favicon.svg":
            return self._static("favicon.svg", "image/svg+xml")
        if p == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if p == "/api/health":
            return self._send(200, {"ok": True})
        if not self._authed():
            if p.startswith("/api/"):
                return self._send(401, {"error": "unauthorized"})
            return self._static("login.html", "text/html; charset=utf-8")
        if p == "/" or p == "/index.html":
            return self._static("index.html", "text/html; charset=utf-8")
        if p == "/chat":
            return self._static("chat.html", "text/html; charset=utf-8")
        if p in ("/app.js", "/chat.js"):
            return self._static(p[1:], "application/javascript; charset=utf-8")
        if p in ("/style.css", "/chat.css"):
            return self._static(p[1:], "text/css; charset=utf-8")
        if p == "/api/sessions":
            return self._send(200, sessions_payload())
        if p == "/api/chat":
            q = parse_qs(parsed.query)
            sid = (q.get("session_id") or [""])[0]
            try:
                offset = int((q.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            meta = INDEX.get(sid)
            if not meta:
                return self._send(404, {"error": "unknown session"})
            out = sessions.read_chat(meta, offset)
            if out is None:
                return self._send(404, {"error": "transcript not found"})
            resolved_sid = meta["session_id"]
            info = live_map().get(resolved_sid)
            out.update({
                "session_id": resolved_sid,
                "raw_id": meta.get("raw_session_id"),
                "provider": meta.get("provider") or "claude",
                "provider_label": meta.get("provider_label") or "Claude",
                "capabilities": sessions.capabilities_for_meta(meta),
                "title": meta.get("title"),
                "project": meta.get("project"),
                "cwd": meta.get("cwd"),
                "active": info is not None,
                "activity": info.get("activity") if info else None,
            })
            return self._send(200, out)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        data = self._body()

        if p == "/api/login":
            if not TOKEN:
                return self._send(400, {"ok": False,
                                        "detail": "no token configured"})
            if hmac.compare_digest((data.get("token") or "").strip(), TOKEN):
                cookie = ("csm_token=%s; Path=/; Max-Age=31536000; "
                          "HttpOnly; SameSite=Lax" % TOKEN)
                return self._send(200, {"ok": True}, extra={"Set-Cookie": cookie})
            time.sleep(0.6)                 # soften brute-force attempts
            return self._send(401, {"ok": False, "detail": "wrong token"})

        if not self._authed():
            return self._send(401, {"error": "unauthorized"})

        if p == "/api/chat/send":
            sid = data.get("session_id")
            text = (data.get("text") or "").strip()
            if not text:
                return self._send(400, {"ok": False, "detail": "empty message"})
            meta = INDEX.get(sid)
            if not meta:
                return self._send(404, {"ok": False, "detail": "unknown session"})
            if not session_capability(meta, "send"):
                return self._send(409, {"ok": False, "unsupported": True,
                                        "detail": "session provider does not support remote send"})
            info = live_map().get(meta["session_id"])
            if not info:
                # not running: the phone UI offers Resume (which opens a fresh
                # terminal on the laptop) and retries once it's live
                return self._send(409, {"ok": False, "inactive": True,
                                        "detail": "session is not running"})
            res = bridge.send_to_session(info["tty"], text)
            return self._send(200 if res["ok"] else 502, res)

        if p == "/api/focus":
            sid = data.get("session_id")
            meta = INDEX.get(sid)
            if not meta:
                return self._send(404, {"ok": False, "detail": "unknown session"})
            if not session_capability(meta, "focus"):
                return self._send(409, {"ok": False, "unsupported": True,
                                        "detail": "session provider does not support focus"})
            active = live_map()
            info = active.get(meta["session_id"])
            if not info:
                return self._send(409, {"ok": False, "detail": "session is no longer active"})
            # pass stable identifiers (session id + title) so the window match
            # doesn't depend on the volatile, animated terminal title.
            info = {**info, "session_id": meta["session_id"],
                    "title": (meta or {}).get("title")}
            return self._send(200, windows.focus_session(info))

        if p == "/api/resume":
            sid = data.get("session_id")
            meta = INDEX.get(sid)
            if not meta:
                return self._send(404, {"ok": False, "detail": "unknown session"})
            if not session_capability(meta, "resume"):
                return self._send(409, {"ok": False, "unsupported": True,
                                        "detail": "session provider does not support resume"})
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
    sys.stderr.write("[csm] serving on %s (auth %s)\n"
                     % (url, "ON" if TOKEN else "off"))
    if HOST not in ("127.0.0.1", "localhost") and not TOKEN:
        sys.stderr.write(
            "[csm] *** WARNING: bound to %s with NO access token. Anyone who "
            "can reach this port can read transcripts and type into your "
            "Claude sessions. Set one:  openssl rand -hex 32 > %s\n"
            % (HOST, TOKEN_FILE))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[csm] shutting down\n")
        httpd.shutdown()


if __name__ == "__main__":
    main()
