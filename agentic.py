"""
agentic.py — the Enter-key "agentic" search.

Instant search (as you type) is a local substring/keyword filter handled in the
browser. When the user commits a query with Enter, we hand the whole session
index to the configured model and let it reason about which past conversations
are actually relevant — matching intent, not just literal tokens — and explain
why.

This is one headless `claude` call over a compact index of every session, so it
is fast and deterministic enough for interactive use while still being a real
LLM judgement rather than keyword matching.
"""

import sessions
from claude_cli import run_claude, extract_json, online, MODEL_SEARCH
from keywords import heuristic_keywords


def _entry(i, meta):
    kws = (meta.get("ai_keywords") or heuristic_keywords(meta, limit=12))[:12]
    first = (meta.get("first_prompt") or "")[:160].replace("\n", " ")
    last = (meta.get("last_prompt") or "")[:120].replace("\n", " ")
    return (
        "[#%d] %s | project:%s | branch:%s\n"
        "     opened: %s\n"
        "     last: %s\n"
        "     keywords: %s"
    ) % (i, meta.get("title") or "(untitled)", meta.get("project") or "?",
         meta.get("git_branch") or "-", first, last, ", ".join(kws))


_PROMPT = """You are a search engine over a developer's archive of past coding assistant sessions.
Each entry below has a number [#n], a title, project, the opening request, the last request, and keywords.

Find the sessions genuinely relevant to the user's query. Match meaning and intent, not just literal words. Rank best first. Return ONLY a JSON array (no prose, no markdown) of up to 12 objects:
  {"ref": <number>, "reason": "<one short clause on why it matches>"}
If nothing is relevant, return [].

USER QUERY: %s

SESSIONS:
%s
"""


def agentic_search(query, metas, model=MODEL_SEARCH, timeout=90):
    """Return {"results": [{session_id, title, project, reason}], "error": str|None}."""
    if not online():
        return {"results": [], "error": "offline"}
    metas = sessions.by_recency(metas)
    index_text = "\n\n".join(_entry(i, m) for i, m in enumerate(metas))
    out = run_claude(_PROMPT % (query, index_text), model=model, timeout=timeout)
    if out is None:
        return {"results": [], "error": "agentic search call failed"}
    arr = extract_json(out, want="array")
    if not isinstance(arr, list):
        return {"results": [], "error": "could not parse model response"}

    results = []
    seen = set()
    for item in arr:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            continue
        if ref < 0 or ref >= len(metas) or ref in seen:
            continue
        seen.add(ref)
        m = metas[ref]
        results.append({
            "session_id": m["session_id"],
            "title": m["title"],
            "project": m["project"],
            "reason": str(item.get("reason", ""))[:200],
        })
    return {"results": results, "error": None}


if __name__ == "__main__":
    import sys
    import sessions
    idx = sessions.SessionIndex(); idx.reindex()
    q = sys.argv[1] if len(sys.argv) > 1 else "window manager and terminal control"
    print("query:", q)
    res = agentic_search(q, idx.all())
    if res["error"]:
        print("error:", res["error"])
    for r in res["results"]:
        print(" • %-46s | %-18s | %s" % (r["title"][:46], r["project"][:18], r["reason"]))
