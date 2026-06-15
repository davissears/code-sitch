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
    return for_name((meta or {}).get("provider") or claude.PROVIDER) or claude
