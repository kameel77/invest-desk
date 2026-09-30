#!/usr/bin/env bash
# Install the Trading Desk as a launchd service (macOS, user-level).
# Survives reboots, crashes and agent sessions — which a background process
# started from a chat session does not (it dies with SIGTERM on session close).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="ai.hermes.tradingdesk"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PY="$ROOT/.venv/bin/python"
PORT="${TD_PORT:-8770}"

if [[ ! -x "$PY" ]]; then
  echo "Brak venv: $PY" >&2
  echo "Najpierw: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>${PORT}</string>
    <string>--log-level</string><string>warning</string>
  </array>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TD_SCAN_INTERVAL</key><string>600</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${ROOT}/logs/server.out</string>
  <key>StandardErrorPath</key><string>${ROOT}/logs/server.err</string>
</dict>
</plist>
PLIST_EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
sleep 3

if curl -s -m 10 -o /dev/null "http://127.0.0.1:${PORT}/api/health"; then
  echo "Trading Desk działa jako usługa: http://127.0.0.1:${PORT}"
  echo "Logi: $ROOT/logs/server.err"
  echo "Stop:  launchctl unload $PLIST"
else
  echo "Nie udało się wystartować. Sprawdź: tail -20 $ROOT/logs/server.err" >&2
  exit 1
fi
