"""
sessions.py — discover and parse Claude Code session logs.

Claude Code stores one JSONL file per conversation under
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl . Each line is one event
(user / assistant / system / ai-title / last-prompt / ...). This module turns
that tree into a list of lightweight session "metas" suitable for a table UI.

Design goals:
  * Never crash on a malformed line or file — degrade to whatever we parsed.
  * Cheap. The big assistant lines can be multiple MB; we peek the line "type"
    with a regex and only json-parse the small lines we actually need.
  * Cached. Parsing is keyed by (mtime, size); unchanged files are never
    re-read. The cache survives restarts on disk.
"""

import os
import re
import glob
import json
import time
import threading
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
STATE_DIR = os.path.join(HOME, ".claude", "situation-monitor")
CACHE_PATH = os.path.join(STATE_DIR, "sessions-cache.json")
# bump when the parsed-meta shape changes, to invalidate stale on-disk caches
CACHE_VERSION = 3

# Lines above this size are almost always heavy assistant turns or tool
# results (big content / base64). We avoid json-parsing them unless we still
# need session metadata, and count their type with a cheap substring check.
_BIG_LINE = 20000

# user lines that are not "real" prompts we want to surface as the opener.
_SKIP_PROMPT_PREFIXES = (
    "<command-name>",
    "<local-command",
    "<command-message>",
    "Caveat:",
    "[Request interrupted",
)


# token usage lives in each assistant event's message.usage. We pull the four
# counters with regexes so we never have to fully parse multi-MB lines. The first
# match per line is the top-level value (the nested "iterations" copy comes later
# and is ignored). "input_tokens" won't match inside "cache_*_input_tokens"
# because those are preceded by '_' not '"'.
_RE_IN = re.compile(rb'"input_tokens":\s*(\d+)')
_RE_OUT = re.compile(rb'"output_tokens":\s*(\d+)')
_RE_CC = re.compile(rb'"cache_creation_input_tokens":\s*(\d+)')
_RE_CR = re.compile(rb'"cache_read_input_tokens":\s*(\d+)')
_RE_TS = re.compile(rb'"timestamp":"([^"]+)"')
_RE_RID = re.compile(rb'"requestId":"([^"]+)"')


def _first_int(regex, raw):
    m = regex.search(raw)
    return int(m.group(1)) if m else 0


def _iso_to_local_day(s):
    """Local calendar date (YYYY-MM-DD) for an ISO-8601 UTC timestamp, or None."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return None


def _local_day(raw):
    """Local day of a raw assistant line's UTC timestamp (regex, no full parse)."""
    m = _RE_TS.search(raw)
    return _iso_to_local_day(m.group(1).decode("ascii", "replace")) if m else None


def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def _text_from_message(message):
    """Pull plain text out of a user/assistant message.content (str or parts)."""
    if message is None:
        return ""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(part.get("text", ""))
        return "\n".join(chunks).strip()
    return ""


def _looks_like_real_prompt(text):
    if not text:
        return False
    for p in _SKIP_PROMPT_PREFIXES:
        if text.startswith(p):
            return False
    return True


def _project_label(cwd, fallback_dir):
    """Friendly project name: last two path components of the cwd."""
    if cwd:
        parts = [p for p in cwd.split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        if parts:
            return parts[-1]
    # fall back to the encoded directory name
    name = (fallback_dir or "").lstrip("-").replace("-", "/")
    return name or "(unknown)"


def parse_session(path):
    """Single-pass parse of one session jsonl -> meta dict (or None)."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    project_dir = os.path.basename(os.path.dirname(path))
    try:
        st = os.stat(path)
    except OSError:
        return None

    ai_title = None
    last_prompt = None
    first_prompt = None
    cwd = None
    git_branch = None
    slug = None
    entrypoint = None
    first_ts = None
    n_user = 0
    n_assistant = 0
    samples = []           # a few real user prompts, for AI keywords / digest
    have_meta = False      # captured cwd/slug/branch/first_ts yet?
    usage_by_day = {}      # "YYYY-MM-DD" -> [input, output, cache_creation, cache_read]
    user_by_day = {}       # "YYYY-MM-DD" -> count of prompts you sent that day
    seen_req = set()       # requestIds already counted (dedupe split responses)

    try:
        with open(path, "rb") as f:
            for raw in f:
                big = len(raw) > _BIG_LINE

                # ---- assistant turns: extract token usage with cheap regexes (no
                # full parse of huge lines), deduped by requestId so split/streamed
                # responses aren't double counted. ----
                if b'"type":"assistant"' in raw:
                    rid_m = _RE_RID.search(raw)
                    rid = rid_m.group(1) if rid_m else None
                    if rid is None or rid not in seen_req:
                        if rid is not None:
                            seen_req.add(rid)
                        n_assistant += 1
                        day = _local_day(raw)
                        if day:
                            b = usage_by_day.get(day)
                            if b is None:
                                b = usage_by_day[day] = [0, 0, 0, 0]
                            b[0] += _first_int(_RE_IN, raw)
                            b[1] += _first_int(_RE_OUT, raw)
                            b[2] += _first_int(_RE_CC, raw)
                            b[3] += _first_int(_RE_CR, raw)
                    if have_meta:
                        continue          # metadata already captured; nothing else to do
                    # else fall through to parse this line for cwd/branch/slug

                # heavy non-assistant lines: skip the expensive parse once we have
                # metadata, unless it's a (rare) big user prompt worth recovering
                elif big and have_meta and not (b'"type":"user"' in raw and b'"toolUseResult"' not in raw):
                    continue

                try:
                    o = json.loads(raw)
                except Exception:
                    continue
                t = o.get("type")

                if t == "ai-title":
                    ai_title = o.get("aiTitle") or ai_title
                    continue
                if t == "last-prompt":
                    if o.get("lastPrompt"):
                        last_prompt = o["lastPrompt"]
                    continue

                # Every conversation line carries session metadata — grab it once.
                if not have_meta:
                    cwd = cwd or o.get("cwd")
                    git_branch = git_branch or o.get("gitBranch")
                    slug = slug or o.get("slug")
                    entrypoint = entrypoint or o.get("entrypoint")
                    first_ts = first_ts or o.get("timestamp")
                    # headless `claude -p` runs (entrypoint "sdk-cli") are automated
                    # calls, not interactive conversations — skip them entirely. This
                    # also keeps the monitor's own AI calls from polluting the list.
                    if entrypoint == "sdk-cli":
                        return None
                    if cwd:
                        have_meta = True

                if t == "assistant":
                    continue              # already counted via regex above

                if t == "user":
                    if o.get("isMeta") or o.get("isSidechain") or o.get("isVisibleInTranscriptOnly"):
                        continue
                    if o.get("toolUseResult") is not None:
                        continue
                    text = _text_from_message(o.get("message"))
                    if _looks_like_real_prompt(text):
                        n_user += 1
                        day = _iso_to_local_day(o.get("timestamp"))
                        if day:
                            user_by_day[day] = user_by_day.get(day, 0) + 1
                        if first_prompt is None:
                            first_prompt = text
                        if len(samples) < 6:
                            samples.append(text[:400])
                    continue
    except OSError:
        return None

    title = ai_title or (first_prompt[:80] if first_prompt else None) or slug or "(untitled session)"

    return {
        "session_id": session_id,
        "path": path,
        "project_dir": project_dir,
        "project": _project_label(cwd, project_dir),
        "cwd": cwd,
        "git_branch": git_branch,
        "slug": slug,
        "title": title,
        "ai_title": ai_title,
        "first_prompt": first_prompt,
        "last_prompt": last_prompt,
        "samples": samples,
        "messages": n_assistant,
        "user_turns": n_user,
        "created": first_ts,
        "updated": st.st_mtime,
        "size": st.st_size,
        "usage_by_day": usage_by_day,
        "user_by_day": user_by_day,
        # filled in later by other modules / cache:
        "ai_keywords": None,
    }


def by_recency(metas):
    """Session metas newest-first by last-updated time (the canonical ordering)."""
    return sorted(metas, key=lambda m: m.get("updated", 0), reverse=True)


def discover_paths():
    """All top-level session jsonls (excludes subagents/ and nested dirs)."""
    paths = []
    if not os.path.isdir(PROJECTS_DIR):
        return paths
    for entry in os.listdir(PROJECTS_DIR):
        d = os.path.join(PROJECTS_DIR, entry)
        if not os.path.isdir(d):
            continue
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            paths.append(p)
    return paths


class SessionIndex:
    """Thread-safe, disk-backed index of all sessions."""

    def __init__(self):
        self._lock = threading.RLock()
        self._by_id = {}          # session_id -> meta
        self._cache = {}          # path -> {key, meta}  (key = "mtime:size")
        self.indexing = False
        self.progress = {"done": 0, "total": 0}
        self._load_cache()

    # ---- cache persistence -------------------------------------------------
    def _load_cache(self):
        try:
            with open(CACHE_PATH) as f:
                data = json.load(f)
            if data.get("version") != CACHE_VERSION:
                self._cache = {}
                return
            self._cache = data.get("by_path", {})
            for entry in self._cache.values():
                meta = entry.get("meta")
                if meta:
                    self._by_id[meta["session_id"]] = meta
        except Exception:
            self._cache = {}

    def _save_cache(self):
        _ensure_state_dir()
        tmp = CACHE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"version": CACHE_VERSION, "by_path": self._cache}, f)
            os.replace(tmp, CACHE_PATH)
        except Exception:
            pass

    # ---- indexing ----------------------------------------------------------
    def reindex(self, on_progress=None):
        """(Re)parse changed/new files; drop deleted ones. Incremental."""
        paths = discover_paths()
        with self._lock:
            self.indexing = True
            self.progress = {"done": 0, "total": len(paths)}
        seen_paths = set()
        changed = False
        for i, path in enumerate(paths):
            seen_paths.add(path)
            try:
                st = os.stat(path)
                key = "%d:%d" % (int(st.st_mtime), st.st_size)
            except OSError:
                continue
            cached = self._cache.get(path)
            if cached and cached.get("key") == key:
                meta = cached["meta"]
                # mtime is the source of truth for "updated"; refresh it cheap
                meta["updated"] = st.st_mtime
            else:
                meta = parse_session(path)
                if meta is None:
                    continue
                # carry forward AI keywords if we had them for this session
                old = self._by_id.get(meta["session_id"])
                if old and old.get("ai_keywords"):
                    meta["ai_keywords"] = old["ai_keywords"]
                with self._lock:
                    self._cache[path] = {"key": key, "meta": meta}
                changed = True
            with self._lock:
                self._by_id[meta["session_id"]] = meta
                self.progress["done"] = i + 1
            if on_progress:
                on_progress(i + 1, len(paths))

        # prune deleted files
        with self._lock:
            for path in list(self._cache.keys()):
                if path not in seen_paths:
                    meta = self._cache[path].get("meta", {})
                    self._by_id.pop(meta.get("session_id", None), None)
                    del self._cache[path]
                    changed = True
            self.indexing = False
        if changed:
            self._save_cache()

    # ---- accessors ---------------------------------------------------------
    def all(self):
        with self._lock:
            return list(self._by_id.values())

    def get(self, session_id):
        with self._lock:
            return self._by_id.get(session_id)

    def set_ai_keywords(self, session_id, keywords):
        with self._lock:
            meta = self._by_id.get(session_id)
            if not meta:
                return
            meta["ai_keywords"] = keywords
            entry = self._cache.get(meta["path"])
            if entry:
                entry["meta"]["ai_keywords"] = keywords
        self._save_cache()


if __name__ == "__main__":
    idx = SessionIndex()
    t0 = time.time()
    idx.reindex()
    metas = sorted(idx.all(), key=lambda m: m["updated"], reverse=True)
    print("indexed %d sessions in %.2fs" % (len(metas), time.time() - t0))
    for m in metas[:12]:
        print(" -", time.strftime("%m-%d %H:%M", time.localtime(m["updated"])),
              "|", (m["title"] or "")[:55].ljust(55), "|", m["project"])
