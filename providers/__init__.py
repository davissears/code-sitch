"""Provider-specific session adapters."""

import config

from . import claude

try:
    from . import codex
except Exception:  # pragma: no cover - provider should degrade if unavailable
    codex = None


def all_providers():
    available = {
        claude.PROVIDER: claude,
    }
    if codex is not None:
        available[codex.PROVIDER] = codex
    return [available[name] for name in config.PROVIDERS if name in available]


def for_name(name):
    for provider in all_providers():
        if getattr(provider, "PROVIDER", None) == name:
            return provider
    return None


def for_meta(meta):
    meta = meta or {}
    provider = for_name(meta.get("provider"))
    if provider:
        return provider
    sid = meta.get("session_id") or ""
    if ":" in sid:
        provider = for_name(sid.split(":", 1)[0])
        if provider:
            return provider
    return claude
