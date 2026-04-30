#!/usr/bin/env bash
# ─── PGMRec — Linux systemd uninstall script ─────────────────────────────────
# Stops and removes the systemd service.  Does NOT remove data or the venv.
#
# Usage (as root or with sudo):
#   sudo bash scripts/linux/uninstall_systemd.sh

set -euo pipefail

SERVICE_FILE="/etc/systemd/system/pgmrec.service"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    exit 1
fi

if systemctl is-active --quiet pgmrec 2>/dev/null; then
    systemctl stop pgmrec
    echo "Service stopped."
fi

if systemctl is-enabled --quiet pgmrec 2>/dev/null; then
    systemctl disable pgmrec
    echo "Service disabled."
fi

if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    echo "Service file removed: $SERVICE_FILE"
fi

systemctl daemon-reload
echo "systemd daemon reloaded."

echo ""
echo "=== PGMRec service removed ==="
echo "Data and venv at /opt/pgmrec are preserved."
echo "Remove manually if no longer needed: rm -rf /opt/pgmrec"
