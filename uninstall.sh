#!/bin/bash
# Stops Work Audit and removes its login item. Your recorded data stays unless you choose to delete it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT="$HOME/Library/LaunchAgents/com.workaudit.app.plist"

pkill -f "workaudit run" 2>/dev/null && echo "Stopped the menu bar app." || true
if [ -f "$AGENT" ]; then
  launchctl bootout "gui/$(id -u)" "$AGENT" 2>/dev/null || true
  rm -f "$AGENT"
  echo "Removed the 'Start at login' item."
fi

read -r -p "Also delete all recorded screenshots, descriptions and reports in $ROOT/data? [y/N] " a </dev/tty || a=""
if [[ "$a" =~ ^[Yy] ]]; then
  rm -rf "$ROOT/data"
  echo "Deleted $ROOT/data."
else
  echo "Kept your data in $ROOT/data."
fi
echo
echo "To finish: delete the folder $ROOT, and remove “Work Audit” from"
echo "System Settings → Privacy & Security → Screen & System Audio Recording (and Automation)."
