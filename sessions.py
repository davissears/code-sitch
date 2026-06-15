"""
sessions.py - provider-agnostic session index.

Provider modules own storage-specific parsing and liveness. This module keeps a
thread-safe, disk-backed index of normalized session metadata for the web API.
"""

import json
import os
import threading
import time

import providers
import config
from providers import claude

STATE_DIR = config.STATE_DIR
CACHE_PATH = os.path.join(STATE_DIR, "sessions-cache.json")
CACHE_VERSION = 4

# Compatibility names for older imports. Claude-specific behavior now lives in
# providers.claude.
PROJECTS_DIR = claude.PROJECTS_DIR
discover_paths = claude.discover_paths
parse_session = claude.parse_session


def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def by_recency(metas):
    """Session metas newest-first by last-updated time."""
    return sorted(metas, key=lambda m: m.get("updated", 0), reverse=True)


def _claude_record(path):
    try:
        st = os.stat(path)
        key = "%d:%d" % (int(st.st_mtime), st.st_size)
    except OSError:
        return None
    return {
        "cache_id": "claude:" + path,
        "key": key,
        "provider": claude,
        "source": path,
    }


def _codex_record(provider, row):
    return {
        "cache_id": provider.cache_id(row),
        "key": provider.cache_signature(row),
        "provider": provider,
        "source": row,
    }


def discover_records():
    """All provider-backed session sources with cache signatures."""
    records = []
    for provider in providers.all_providers():
        if provider is claude:
            for path in claude.discover_paths():
                rec = _claude_record(path)
                if rec:
                    records.append(rec)
        else:
            discover = getattr(provider, "discover_threads", None)
            if not discover:
                continue
            for row in discover():
                records.append(_codex_record(provider, row))
    return records


def provider_for_meta(meta):
    return providers.for_meta(meta)


def capabilities_for_meta(meta):
    """Return a fresh API capabilities value for a provider meta."""
    provider = provider_for_meta(meta)
    if provider:
        fn = getattr(provider, "capabilities", None)
        if fn:
            return fn()
        caps = getattr(provider, "CAPABILITIES", None)
        if isinstance(caps, dict):
            return dict(caps)
        if isinstance(caps, (list, tuple)):
            return list(caps)
    caps = meta.get("capabilities")
    if isinstance(caps, dict):
        return dict(caps)
    if isinstance(caps, (list, tuple)):
        return list(caps)
    return {}


def ensure_session_metadata(meta):
    """Backfill provider metadata added after older cache entries were written."""
    provider = provider_for_meta(meta)
    if provider:
        raw_fn = getattr(provider, "raw_session_id", None)
        qualify_fn = getattr(provider, "qualify_session_id", None)
        if "provider" not in meta:
            meta["provider"] = provider.PROVIDER
        if "provider_label" not in meta:
            meta["provider_label"] = getattr(provider, "PROVIDER_LABEL", provider.PROVIDER)
        if raw_fn and not meta.get("raw_session_id"):
            meta["raw_session_id"] = raw_fn(meta)
        if qualify_fn:
            raw_id = meta.get("raw_session_id") or meta.get("session_id")
            meta["session_id"] = qualify_fn(raw_id)
    if "capabilities" not in meta:
        meta["capabilities"] = capabilities_for_meta(meta)
    return meta


def transcript_path(meta):
    provider = provider_for_meta(meta)
    return provider.transcript_path(meta) if provider else None


def read_chat(meta, offset=0):
    provider = provider_for_meta(meta)
    path = provider.transcript_path(meta) if provider else None
    if not path:
        return None
    return provider.read_chat(path, offset)


def active_sessions(metas):
    """Merge live-session maps from providers that support liveness."""
    live = {}
    by_provider = {}
    for meta in metas:
        by_provider.setdefault(meta.get("provider") or claude.PROVIDER, []).append(meta)
    for provider in providers.all_providers():
        fn = getattr(provider, "active_sessions", None)
        if fn:
            live.update(fn(by_provider.get(provider.PROVIDER, [])))
    return live


class SessionIndex:
    """Thread-safe, disk-backed index of all provider sessions."""

    def __init__(self):
        self._lock = threading.RLock()
        self._by_id = {}
        self._aliases = {}
        self._cache = {}
        self.indexing = False
        self.progress = {"done": 0, "total": 0}
        self._load_cache()

    def _remember_meta(self, meta):
        ensure_session_metadata(meta)
        sid = meta["session_id"]
        self._by_id[sid] = meta
        if meta.get("provider") == claude.PROVIDER and meta.get("raw_session_id"):
            self._aliases[meta["raw_session_id"]] = sid

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
                    self._remember_meta(meta)
        except Exception:
            self._cache = {}

    def _save_cache(self):
        try:
            _ensure_state_dir()
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": CACHE_VERSION, "by_path": self._cache}, f)
            os.replace(tmp, CACHE_PATH)
        except Exception:
            pass

    # ---- indexing ----------------------------------------------------------
    def reindex(self, on_progress=None):
        """(Re)parse changed/new provider sources; drop deleted ones."""
        records = discover_records()
        with self._lock:
            old_by_id = dict(self._by_id)
            self.indexing = True
            self.progress = {"done": 0, "total": len(records)}
            self._by_id = {}
            self._aliases = {}

        seen_sources = set()
        changed = False
        for i, rec in enumerate(records):
            source_id = rec["cache_id"]
            seen_sources.add(source_id)
            cached = self._cache.get(source_id)
            if cached and cached.get("key") == rec["key"]:
                meta = cached["meta"]
            else:
                meta = rec["provider"].parse_session(rec["source"])
                if meta is None:
                    if source_id in self._cache:
                        del self._cache[source_id]
                        changed = True
                    continue
                old = old_by_id.get(meta["session_id"])
                if old and old.get("ai_keywords"):
                    meta["ai_keywords"] = old["ai_keywords"]
                with self._lock:
                    self._cache[source_id] = {"key": rec["key"], "meta": meta}
                changed = True

            with self._lock:
                self._remember_meta(meta)
                self.progress["done"] = i + 1
            if on_progress:
                on_progress(i + 1, len(records))

        with self._lock:
            for source_id in list(self._cache.keys()):
                if source_id not in seen_sources:
                    del self._cache[source_id]
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
            sid = self._aliases.get(session_id, session_id)
            return self._by_id.get(sid)

    def set_ai_keywords(self, session_id, keywords):
        with self._lock:
            sid = self._aliases.get(session_id, session_id)
            meta = self._by_id.get(sid)
            if not meta:
                return
            meta["ai_keywords"] = keywords
            for entry in self._cache.values():
                cached = entry.get("meta") or {}
                if cached.get("session_id") == meta["session_id"]:
                    cached["ai_keywords"] = keywords
                    break
        self._save_cache()


if __name__ == "__main__":
    idx = SessionIndex()
    t0 = time.time()
    idx.reindex()
    metas = by_recency(idx.all())
    print("indexed %d sessions in %.2fs" % (len(metas), time.time() - t0))
    for m in metas[:12]:
        print(" -", time.strftime("%m-%d %H:%M", time.localtime(m["updated"])),
              "|", (m["title"] or "")[:55].ljust(55), "|",
              m.get("provider_label") or m.get("provider"), "|", m["project"])
