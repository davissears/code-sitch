"""Provider-specific session adapters."""

from . import claude

try:
    from . import codex
except Exception:  # pragma: no cover - provider should degrade if unavailable
    codex = None


def all_providers():
    providers = [claude]
    if codex is not None:
        providers.append(codex)
    return providers


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
