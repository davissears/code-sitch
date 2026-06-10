#!/usr/bin/env bash
# Install the Situation Monitor as a macOS LaunchAgent that starts at login and
# restarts on crash. All paths are resolved for THIS machine — nothing hardcoded.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.claude-situation-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${CSM_PORT:-8787}"
HOST="${CSM_HOST:-127.0.0.1}"
LOG="$HOME/.claude/situation-monitor/server.log"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "error: python3 not found on PATH" >&2; exit 1; }

# LaunchAgents get a minimal PATH; spell out where claude/yabai/system tools live.
EXTRA=""
for b in claude yabai; do
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
if [ "$HOST" != "127.0.0.1" ] && [ ! -s "$HOME/.claude/situation-monitor/token" ] && [ -z "${CSM_TOKEN:-}" ]; then
  echo "WARNING: bound beyond localhost with no access token — see REMOTE.md."
fi
echo "Remove with: ./uninstall-startup.sh"
