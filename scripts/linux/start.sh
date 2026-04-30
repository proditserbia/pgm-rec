#!/usr/bin/env bash
# ─── PGMRec — Start (non-Docker, dev/venv mode) ──────────────────────────────
# Starts the backend directly using the local venv.
# Assumes you are in the repository root.
#
# PGMRec is a LAN-only application.
# Default: binds to 127.0.0.1 (local machine only).
# For LAN access from other machines:
#   - Set PGMREC_HOST=0.0.0.0 in .env  OR  pass --host 0.0.0.0
#   - Also set PGMREC_CORS_ORIGINS to include your server's LAN IP in .env
#
# Usage:
#   bash scripts/linux/start.sh
#   bash scripts/linux/start.sh --port 8001
#   bash scripts/linux/start.sh --host 0.0.0.0   # expose to LAN

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
BACKEND_DIR="$REPO_DIR/backend"
VENV_DIR="$REPO_DIR/.venv"

# Load .env first so PGMREC_HOST / PGMREC_PORT can be set there
if [[ -f "$REPO_DIR/.env" ]]; then
    set -a; source "$REPO_DIR/.env"; set +a
fi

# Defaults: read from env (set by .env above or caller), fall back to safe LAN defaults
HOST="${PGMREC_HOST:-127.0.0.1}"
PORT="${PGMREC_PORT:-8000}"

# Allow override via CLI args (highest priority)
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Find uvicorn: prefer local venv, fall back to system
if [[ -x "$VENV_DIR/bin/uvicorn" ]]; then
    UVICORN="$VENV_DIR/bin/uvicorn"
elif command -v uvicorn &>/dev/null; then
    UVICORN="uvicorn"
else
    echo "ERROR: uvicorn not found. Create venv: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt" >&2
    exit 1
fi

cd "$BACKEND_DIR"
echo "Starting PGMRec on http://$HOST:$PORT  (LAN-only — do not expose to internet)"
exec "$UVICORN" app.main:app --host "$HOST" --port "$PORT" --workers 1
