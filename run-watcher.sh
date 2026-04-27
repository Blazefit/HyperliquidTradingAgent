#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/HyperliquidTradingAgent"
PYTHON="$REPO_DIR/.venv/bin/python"
LOG_DIR="$REPO_DIR/logs"
LOG="$LOG_DIR/watcher.log"

mkdir -p "$LOG_DIR"
cd "$REPO_DIR"

while true; do
    printf '\n[%s] starting manual_setup watcher\n' "$(date --iso-8601=seconds)" >> "$LOG"
    "$PYTHON" manual_setup.py --asset BTC --direction long --entry 75507 --sl 73997 --tp1 76615,25 --tp2 78384,35 --tp3 79644,20 --trail-sl 78384 --trail-after 2 >> "$LOG" 2>&1 || true
    printf '[%s] watcher exited, restarting in 15 seconds\n' "$(date --iso-8601=seconds)" >> "$LOG"
    sleep 15
done
