#!/usr/bin/env bash
# Stop and remove the Situation Monitor LaunchAgent installed by install-startup.sh.
set -euo pipefail

LABEL="com.claude-situation-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Uninstalled $LABEL (the running server, if any, has been stopped)."
