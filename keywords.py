"""
keywords.py — searchable keyword pools for each session.

Two layers:
  * heuristic_keywords(): instant, derived from the fields we already parsed
    (title, prompts, project, branch). Always available, zero latency.
  * ai_keywords(): a richer pool generated from a compact digest of the
    conversation. Generated lazily in the background and cached forever.

The instant search in the UI matches against the union of both plus the raw
title/project/prompt text, so search is useful immediately and gets better as
AI keywords arrive.
"""

import re
import json
import threading

import sessions
from claude_cli import run_claude, extract_json, online, MODEL_KEYWORDS

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_+#.-]{1,}")

STOPWORDS = set("""
the a an and or but if then else for to of in on at by with from into over under
is are was were be been being do does did done have has had will would can could
should may might must this that these those it its as not no yes you your i me my we
our they them he she his her about up out off so than too very just only also more
most some any each which who whom what when where why how please make want need use
using used get got let new old run running ran via per etc into onto your re
""".split())


def _tokens(text):
    if not text:
        return []
    return [w.lower() for w in _WORD.findall(text)]


def heuristic_keywords(meta, limit=24):
    """Salient terms from already-parsed fields, frequency-ordered."""
    fields = [
        meta.get("title", ""),
        meta.get("project", ""),
        meta.get("git_branch", "") or "",
        meta.get("first_prompt", "") or "",
        meta.get("last_prompt", "") or "",
        " ".join(meta.get("samples", []) or []),
    ]
    # project / branch path components are strong signals
    for sep in ("/", "-", "_"):
        fields.append((meta.get("project", "") or "").replace(sep, " "))
        fields.append((meta.get("git_branch", "") or "").replace(sep, " "))

    counts = {}
    for f in fields:
        for tok in _tokens(f):
            if tok in STOPWORDS or len(tok) < 2:
                continue
            counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def build_digest(meta, max_chars=1600):
    """Compact text describing the session for the AI keyword prompt."""
    lines = [
        "Title: " + (meta.get("title") or ""),
        "Project: " + (meta.get("project") or ""),
        "Branch: " + (meta.get("git_branch") or ""),
    ]
    if meta.get("first_prompt"):
        lines.append("Opening request: " + meta["first_prompt"][:500])
    for i, s in enumerate(meta.get("samples", [])[1:5], 1):
        lines.append("Later message %d: %s" % (i, s[:250]))
    if meta.get("last_prompt"):
        lines.append("Last request: " + meta["last_prompt"][:300])
    return "\n".join(lines)[:max_chars]


_AI_PROMPT = """You generate search keywords for a developer's archive of past coding assistant sessions.

Given the session summary below, output ONLY a JSON array of 10-18 lowercase search keywords and short phrases that someone might later type to find THIS session. Include: technologies, file/tool names, the domain/topic, the kind of task (e.g. "bug fix", "refactor", "data pipeline"), and salient nouns. No prose, no markdown — just the JSON array.

SESSION:
%s
"""


def ai_keywords(meta, model=MODEL_KEYWORDS, timeout=60):
    """Ask the configured model for a keyword pool. Returns list[str] or None on failure."""
    digest = build_digest(meta)
    if not digest.strip():
        return None
    out = run_claude(_AI_PROMPT % digest, model=model, timeout=timeout)
    arr = extract_json(out, want="array")
    if not isinstance(arr, list):
        return None
    kws = []
    for x in arr:
        if isinstance(x, str):
            x = x.strip().lower()
            if x and len(x) < 40:
                kws.append(x)
    return kws[:20] or None


class KeywordEnricher:
    """Background worker: fills in AI keywords for sessions that lack them."""

    def __init__(self, index, limit=40, model=MODEL_KEYWORDS):
        self.index = index
        self.limit = limit            # cap how many sessions we enrich per pass
        self.model = model
        self._stop = threading.Event()
        self._thread = None
        self._fails = {}              # session_id -> consecutive failure count
        self.status = {"running": False, "done": 0, "pending": 0}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        self.status["running"] = True
        try:
            # newest sessions first — most likely to be searched
            metas = sessions.by_recency(self.index.all())
            todo = [m for m in metas if not m.get("ai_keywords")
                    and self._fails.get(m["session_id"], 0) < 3
                    and (m.get("first_prompt") or m.get("ai_title"))]
            todo = todo[: self.limit]
            self.status["pending"] = len(todo)
            done = 0
            for m in todo:
                if self._stop.is_set():
                    break
                # offline: abandon this pass without penalising sessions; the
                # maintenance loop will start a fresh pass once the network is back
                if not online():
                    break
                kws = ai_keywords(m, model=self.model)
                if kws:
                    self.index.set_ai_keywords(m["session_id"], kws)
                    self._fails.pop(m["session_id"], None)
                else:
                    self._fails[m["session_id"]] = self._fails.get(m["session_id"], 0) + 1
                done += 1
                self.status["done"] = done
                self.status["pending"] = len(todo) - done
        finally:
            self.status["running"] = False


if __name__ == "__main__":
    import sessions
    idx = sessions.SessionIndex(); idx.reindex()
    m = sorted(idx.all(), key=lambda x: x["updated"], reverse=True)[0]
    print("session:", m["title"])
    print("heuristic:", heuristic_keywords(m))
    print("digest:\n", build_digest(m))
    print("ai:", ai_keywords(m))
