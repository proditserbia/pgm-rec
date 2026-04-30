#!/usr/bin/env bash
# ─── PGMRec — Stop (non-Docker, dev/venv mode) ───────────────────────────────
# Sends SIGTERM to a running uvicorn/PGMRec process by port.
#
# Usage:
#   bash scripts/linux/stop.sh
#   bash scripts/linux/stop.sh --port 8001

set -euo pipefail

PORT="${PGMREC_PORT:-8000}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Find PID listening on the given port
PID=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)

if [[ -z "$PID" ]]; then
    echo "No process found on port $PORT."
    exit 0
fi

echo "Stopping PGMRec (PID $PID) on port $PORT…"
kill -TERM "$PID"

# Wait up to 15 s for graceful shutdown
for i in $(seq 1 15); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "PGMRec stopped."
        exit 0
    fi
    sleep 1
done

echo "Process did not stop after 15 s — sending SIGKILL."
kill -KILL "$PID" 2>/dev/null || true
echo "Done."
