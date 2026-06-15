#!/usr/bin/env bash
# Install the Situation Monitor as a macOS LaunchAgent that starts at login and
# restarts on crash. All paths are resolved for THIS machine — nothing hardcoded.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.claude-situation-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${CSM_PORT:-8787}"
HOST="${CSM_HOST:-127.0.0.1}"
PROVIDERS="${CSM_PROVIDERS:-claude,codex}"
CLAUDE_HOME="${CSM_CLAUDE_HOME:-$HOME/.claude}"
CODEX_HOME="${CSM_CODEX_HOME:-$HOME/.codex}"
CODEX_STATE_DB="${CSM_CODEX_STATE_DB:-$CODEX_HOME/sqlite/state_5.sqlite}"
LEGACY_STATE="$HOME/.claude/situation-monitor"
if [ -n "${CSM_STATE_DIR:-}" ]; then
  STATE_DIR="$CSM_STATE_DIR"
elif [ -d "$LEGACY_STATE" ]; then
  STATE_DIR="$LEGACY_STATE"
else
  STATE_DIR="$HOME/.situation-monitor"
fi
LOG="$STATE_DIR/server.log"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "error: python3 not found on PATH" >&2; exit 1; }
CLAUDE_CLI="${CSM_CLAUDE_CLI:-$(command -v claude 2>/dev/null || true)}"
[ -n "$CLAUDE_CLI" ] || CLAUDE_CLI="$HOME/.local/bin/claude"
CODEX_CLI="${CSM_CODEX_CLI:-$(command -v codex 2>/dev/null || true)}"
[ -n "$CODEX_CLI" ] || CODEX_CLI="codex"

# LaunchAgents get a minimal PATH; spell out where provider CLIs, yabai, and
# system tools live.
EXTRA=""
for b in claude codex yabai; do
  p="$(command -v "$b" 2>/dev/null || true)"
  [ -n "$p" ] && EXTRA="$EXTRA:$(dirname "$p")"
done
AGENT_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin${EXTRA}:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$(dirname "$LOG")" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$HERE/server.py</string>
    </array>
    <key>WorkingDirectory</key><string>$HERE</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$AGENT_PATH</string>
        <key>CSM_PORT</key><string>$PORT</string>
        <key>CSM_HOST</key><string>$HOST</string>
        <key>CSM_PROVIDERS</key><string>$PROVIDERS</string>
        <key>CSM_STATE_DIR</key><string>$STATE_DIR</string>
        <key>CSM_CLAUDE_HOME</key><string>$CLAUDE_HOME</string>
        <key>CSM_CODEX_HOME</key><string>$CODEX_HOME</string>
        <key>CSM_CODEX_STATE_DB</key><string>$CODEX_STATE_DB</string>
        <key>CSM_CLAUDE_CLI</key><string>$CLAUDE_CLI</string>
        <key>CSM_CODEX_CLI</key><string>$CODEX_CLI</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>$LOG</string>
    <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL" || true

echo "Installed $LABEL"
echo "  → http://$HOST:$PORT/"
echo "  logs: $LOG"
if [ "$HOST" != "127.0.0.1" ] && [ ! -s "$STATE_DIR/token" ] && [ -z "${CSM_TOKEN:-}" ]; then
  echo "WARNING: bound beyond localhost with no access token — see REMOTE.md."
fi
echo "Remove with: ./uninstall-startup.sh"
