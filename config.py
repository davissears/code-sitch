"""Configuration defaults for the Situation Monitor."""

import os
import shutil

HOME = os.path.expanduser("~")

CLAUDE_HOME = os.path.expanduser(
    os.environ.get("CSM_CLAUDE_HOME", os.path.join(HOME, ".claude"))
)
CODEX_HOME = os.path.expanduser(
    os.environ.get("CSM_CODEX_HOME", os.path.join(HOME, ".codex"))
)

_LEGACY_STATE_DIR = os.path.join(HOME, ".claude", "situation-monitor")
STATE_DIR = os.path.expanduser(
    os.environ.get(
        "CSM_STATE_DIR",
        _LEGACY_STATE_DIR if os.path.isdir(_LEGACY_STATE_DIR)
        else os.path.join(HOME, ".situation-monitor"),
    )
)

PROVIDERS = tuple(
    p.strip().lower()
    for p in os.environ.get("CSM_PROVIDERS", "claude,codex").split(",")
    if p.strip()
)

CLAUDE_PROJECTS_DIR = os.path.join(CLAUDE_HOME, "projects")
CODEX_STATE_DB = os.path.expanduser(
    os.environ.get(
        "CSM_CODEX_STATE_DB",
        os.path.join(CODEX_HOME, "sqlite", "state_5.sqlite"),
    )
)

CLAUDE_CLI = os.path.expanduser(
    os.environ.get("CSM_CLAUDE_CLI")
    or shutil.which("claude")
    or os.path.join(HOME, ".local", "bin", "claude")
)
CODEX_CLI = os.path.expanduser(
    os.environ.get("CSM_CODEX_CLI")
    or shutil.which("codex")
    or "codex"
)
