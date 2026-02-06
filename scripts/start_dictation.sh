#!/usr/bin/env bash
# Complete WhisperLiveKit dictation startup script

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

echo "[Dictation] Starting WhisperLiveKit system..."

# 1. Check if server is running, start if needed
if ! pgrep -f "whisperlivekit-server" >/dev/null; then
    echo "[Server] Starting WhisperLiveKit server..."
    "$SCRIPT_DIR/start_server.sh"
    sleep 2
else
    echo "[Server] Already running"
fi

# 2. Check if a typing backend is available
if command -v ydotool >/dev/null 2>&1; then
    if systemctl is-active --quiet ydotoold; then
        echo "[Typing] ydotoold is active"
    else
        echo "[Typing] ydotoold is not active; attempting start (may prompt for sudo)"
        sudo systemctl start ydotoold || true
    fi
fi

# 3. Start auto-type client
echo "[Client] Starting auto-type client..."
exec "$ROOT_DIR/bin/auto-type" --whisper-url ws://localhost:8888/asr "$@"
