#!/usr/bin/env bash
# Stop WhisperLiveKit dictation system

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[Dictation] Stopping system..."

# Stop auto-type client
pkill -f "auto_type_client.py" 2>/dev/null || true

# Stop server
"$SCRIPT_DIR/stop_server.sh" || true

echo "[Dictation] Stopped"
