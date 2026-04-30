#!/usr/bin/env bash
# ─── PGMRec — Linux systemd install script ───────────────────────────────────
# Installs PGMRec as a systemd service on Ubuntu/Debian.
#
# Usage (as root or with sudo):
#   sudo bash scripts/linux/install_systemd.sh
#
# What it does:
#   1. Creates /opt/pgmrec with the right layout
#   2. Creates a 'pgmrec' system user
#   3. Sets up a Python venv and installs dependencies
#   4. Installs and enables the systemd unit
#   5. Writes a .env template if one does not exist
#
# Pre-requisites:
#   - Python 3.12+ installed
#   - FFmpeg installed (apt install ffmpeg)
#   - Run from the repository root

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_DIR="/opt/pgmrec"
SERVICE_USER="pgmrec"
SERVICE_FILE="/etc/systemd/system/pgmrec.service"
VENV_DIR="$INSTALL_DIR/.venv"

echo "=== PGMRec systemd installer ==="
echo "Repository : $REPO_DIR"
echo "Install dir: $INSTALL_DIR"

# ── 1. Check root ─────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)." >&2
    exit 1
fi

# ── 2. Create system user ─────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "Created system user: $SERVICE_USER"
else
    echo "User $SERVICE_USER already exists — skipping."
fi

# ── 3. Install layout ─────────────────────────────────────────────────────────
mkdir -p \
    "$INSTALL_DIR/backend" \
    "$INSTALL_DIR/data/channels" \
    "$INSTALL_DIR/data/manifests" \
    "$INSTALL_DIR/data/exports" \
    "$INSTALL_DIR/data/preview" \
    "$INSTALL_DIR/logs/exports"

# Copy backend source
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
    "$REPO_DIR/backend/" "$INSTALL_DIR/backend/"

# Optional: copy pre-built frontend
if [[ -d "$REPO_DIR/frontend/dist" ]]; then
    mkdir -p "$INSTALL_DIR/frontend"
    rsync -a "$REPO_DIR/frontend/dist/" "$INSTALL_DIR/frontend/dist/"
    echo "Frontend build copied to $INSTALL_DIR/frontend/dist"
else
    echo "NOTE: No frontend/dist found. Run 'cd frontend && npm run build' first."
fi

# ── 4. Python venv ────────────────────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    echo "Created venv at $VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/backend/requirements.txt"
echo "Python dependencies installed."

# ── 5. .env file ──────────────────────────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$REPO_DIR/.env.example" "$INSTALL_DIR/.env"
    echo "Created $INSTALL_DIR/.env from template."
    echo "⚠️  Edit $INSTALL_DIR/.env and set PGMREC_JWT_SECRET_KEY and PGMREC_ADMIN_PASSWORD."
else
    echo ".env already exists — not overwritten."
fi

# ── 6. Ownership ──────────────────────────────────────────────────────────────
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── 7. systemd unit ───────────────────────────────────────────────────────────
# Patch WorkingDirectory / venv path into service file
sed \
    -e "s|/opt/pgmrec|$INSTALL_DIR|g" \
    "$REPO_DIR/scripts/linux/pgmrec.service" \
    > "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable pgmrec.service
echo "systemd unit installed and enabled."

echo ""
echo "=== Installation complete ==="
echo "  Edit env : $INSTALL_DIR/.env"
echo "  Start    : sudo systemctl start pgmrec"
echo "  Status   : sudo systemctl status pgmrec"
echo "  Logs     : sudo journalctl -u pgmrec -f"
