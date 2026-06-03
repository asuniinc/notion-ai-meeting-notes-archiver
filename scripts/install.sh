#!/bin/zsh
set -euo pipefail

SERVICE="notion-ai-meeting-notes-archiver"
LABEL="com.local.notion-ai-meeting-notes-archiver"
APP_DIR="$HOME/Library/Application Support/Notion AI Meeting Notes Archiver"
LOG_DIR="$HOME/Library/Logs/Notion AI Meeting Notes Archiver"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
SOURCE_DIR="${0:A:h}/.."
PYTHON="${PYTHON:-$(command -v python3)}"

if [[ -z "$PYTHON" ]]; then
  print -u2 "python3 was not found."
  exit 1
fi

mkdir -p "$APP_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
cp "$SOURCE_DIR/notion_ai_meeting_notes_archiver.py" "$APP_DIR/"
cp "$SOURCE_DIR/config.example.json" "$APP_DIR/config.example.json"
cp "$SOURCE_DIR/README.md" "$APP_DIR/README.md"

if [[ ! -f "$APP_DIR/config.json" ]]; then
  cp "$SOURCE_DIR/config.example.json" "$APP_DIR/config.json"
fi

TOKEN="${NOTION_PAT:-}"
if [[ -z "$TOKEN" && -t 0 ]]; then
  print -n "Paste your Notion PAT (hidden; leave blank to keep existing Keychain token): "
  stty -echo
  read TOKEN
  stty echo
  print
elif [[ -z "$TOKEN" ]]; then
  print "No TTY detected; keeping any existing Keychain token."
fi

if [[ -n "$TOKEN" ]]; then
  security add-generic-password -a "$USER" -s "$SERVICE" -w "$TOKEN" -U >/dev/null
  print "Stored Notion PAT in macOS Keychain service '$SERVICE'."
fi

IGNORE_BEFORE="$("$PYTHON" -c 'import datetime as dt; print(dt.datetime.now().astimezone().isoformat(timespec="seconds"))')"

cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-u</string>
    <string>$APP_DIR/notion_ai_meeting_notes_archiver.py</string>
    <string>--config</string>
    <string>$APP_DIR/config.json</string>
    <string>--ignore-before</string>
    <string>$IGNORE_BEFORE</string>
    <string>watch</string>
    <string>--upload</string>
    <string>--interval</string>
    <string>60</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/notion-ai-meeting-notes-archiver.out.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/notion-ai-meeting-notes-archiver.err.log</string>
</dict>
</plist>
PLIST

plutil -lint "$LAUNCH_AGENT" >/dev/null
launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

"$PYTHON" "$APP_DIR/notion_ai_meeting_notes_archiver.py" \
  --config "$APP_DIR/config.json" \
  doctor

print "Installed $LABEL."
