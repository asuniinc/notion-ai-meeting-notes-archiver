#!/bin/zsh
set -euo pipefail

SERVICE="notion-ai-meeting-notes-archiver"
LABEL="com.local.notion-ai-meeting-notes-archiver"
APP_DIR="$HOME/Library/Application Support/Notion AI Meeting Notes Archiver"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT" 2>/dev/null || true
rm -f "$LAUNCH_AGENT"

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$APP_DIR"
  security delete-generic-password -s "$SERVICE" 2>/dev/null || true
  print "Removed app directory and Keychain token."
else
  print "Kept app directory, archive, manifest, and Keychain token."
fi

print "Uninstalled $LABEL."
