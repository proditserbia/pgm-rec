# PGMRec — Broadcast Recording & Compliance System

PGMRec is a broadcast-grade SDI/HDMI capture and compliance recording system
built on FFmpeg + FastAPI + React.

> **LAN-only deployment** — PGMRec is designed for internal broadcast/server
> networks. It is not intended to be exposed to the public internet.
> All features work fully offline on a local network.

## Architecture

```
backend/   FastAPI + SQLite backend (uvicorn)
frontend/  React 18 + TypeScript + Vite web UI
scripts/   Deployment helpers (Linux + Windows)
docs/      Configuration examples
```

---

## Network model

| Scenario | How to deploy |
|----------|--------------|
| Single machine | Default — backend binds to `127.0.0.1:8000` |
| LAN access (other machines) | Start backend with `--host 0.0.0.0`; set `PGMREC_CORS_ORIGINS` to your server's LAN IP |
| Hostname instead of IP (optional) | Add Nginx/Caddy reverse proxy (`docs/nginx.conf.example`) |
| HTTPS on LAN (optional) | Use a self-signed cert or internal CA with Nginx |

No internet access is required at runtime.

---

## Recommended directory layout

| Path | Purpose |
|------|---------|
| `data/channels/` | Channel JSON configs |
| `data/manifests/` | Daily segment manifests |
| `data/exports/` | Exported video files |
| `data/preview/` | HLS preview segments |
| `logs/` | Backend + FFmpeg logs |
| `pgmrec.db` | SQLite database |

---

## 1. Linux — Docker (recommended)

### Prerequisites

- Docker ≥ 24 and Docker Compose ≥ 2

```bash
# 1. Clone repository
git clone https://github.com/proditserbia/pgm-rec.git
cd pgm-rec

# 2. Build frontend (optional — embeds UI into the backend container)
cd frontend && npm install && npm run build && cd ..

# 3. Configure
cp .env.example .env
# Edit .env:
#   PGMREC_JWT_SECRET_KEY=<long random string>
#   PGMREC_ADMIN_PASSWORD=<strong password>
#   PGMREC_CORS_ORIGINS=http://localhost:8000

# 4. Start
docker compose up -d

# 5. Open browser on this machine
# http://localhost:8000
```

### LAN access from other machines

```bash
# In .env:
PGMREC_CORS_ORIGINS=http://192.168.1.10:8000   # your server's LAN IP

# In docker-compose.yml, change the ports line to:
#   - "0.0.0.0:8000:8000"

docker compose up -d
# Access from LAN: http://192.168.1.10:8000
```

### Data volumes

Docker Compose creates two named volumes (`pgmrec-data`, `pgmrec-logs`).
To use host paths instead, edit `docker-compose.yml`:

```yaml
volumes:
  - /opt/pgmrec/data:/app/data
  - /opt/pgmrec/logs:/app/logs
```

### Updating

```bash
git pull
cd frontend && npm run build && cd ..
docker compose build
docker compose up -d
```

---

## 2. Linux — systemd (non-Docker)

### Prerequisites

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ffmpeg rsync
```

> **Blackmagic / DeckLink** — install the DeckLink driver from
> https://www.blackmagicdesign.com/support  
> The `decklink` FFmpeg input device must be compiled into your FFmpeg build.
> No internet access is needed after driver installation.

### Install

```bash
sudo bash scripts/linux/install_systemd.sh
```

The script creates `/opt/pgmrec/`, a `pgmrec` system user, a Python venv,
installs deps, and registers the `pgmrec.service` systemd unit.

### Configure

```bash
sudo nano /opt/pgmrec/.env
# Required:
#   PGMREC_JWT_SECRET_KEY=<long random string>
#   PGMREC_ADMIN_PASSWORD=<strong password>
#   PGMREC_CORS_ORIGINS=http://localhost:8000
```

### Control

```bash
sudo systemctl start pgmrec
sudo systemctl stop pgmrec
sudo systemctl status pgmrec
sudo journalctl -u pgmrec -f        # live logs

# Convenience wrappers (run from repo root, binds 127.0.0.1 by default):
bash scripts/linux/start.sh
bash scripts/linux/stop.sh
```

### LAN access (systemd)

Edit the `ExecStart` line in `/etc/systemd/system/pgmrec.service`:

```ini
ExecStart=/opt/pgmrec/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Also set `PGMREC_CORS_ORIGINS=http://192.168.1.10:8000` in `/opt/pgmrec/.env`.

```bash
sudo systemctl daemon-reload && sudo systemctl restart pgmrec
```

### Uninstall

```bash
sudo bash scripts/linux/uninstall_systemd.sh
```

---

## 3. Windows — Service (NSSM)

### Prerequisites

- Python 3.12+ on `PATH`
- FFmpeg installed (e.g. `C:\AutoRec\ffmpeg\bin\`)
- PowerShell 5+, run as Administrator
- NSSM (Non-Sucking Service Manager):
  - **Automatic** (requires internet once during setup): the installer downloads it
  - **Offline**: place `nssm.exe` in `scripts\windows\nssm.exe` before running
    (download from https://nssm.cc on any machine)

> **DeckLink on Windows** — install the Blackmagic Design DeckLink driver.
> Point `PGMREC_FFMPEG_PATH_OVERRIDE` to an FFmpeg build compiled with `decklink`.

### Install

```powershell
# Run PowerShell as Administrator from repository root

# Local machine only (default — binds 127.0.0.1)
Set-ExecutionPolicy Bypass -Scope Process
.\scripts\windows\install_service.ps1

# LAN access (binds 0.0.0.0 — also set PGMREC_CORS_ORIGINS in .env)
.\scripts\windows\install_service.ps1 -Host 0.0.0.0
```

### Configure

```
notepad C:\PGMRec\.env
```

Minimum required settings:

```env
PGMREC_JWT_SECRET_KEY=<long random string>
PGMREC_ADMIN_PASSWORD=<strong password>
PGMREC_CORS_ORIGINS=http://localhost:8000

# FFmpeg paths (adjust to your install location)
PGMREC_FFMPEG_PATH_OVERRIDE=C:\AutoRec\ffmpeg\bin\ffmpeg.exe
PGMREC_FFPROBE_PATH=C:\AutoRec\ffmpeg\bin\ffprobe.exe

# Data directories
PGMREC_DATA_DIR=C:\PGMRec\data
PGMREC_LOGS_DIR=C:\PGMRec\logs
PGMREC_DATABASE_URL=sqlite:///C:/PGMRec/data/pgmrec.db
```

### Control

```powershell
.\scripts\windows\start_service.ps1
.\scripts\windows\stop_service.ps1
# Or: sc start PGMRec / sc stop PGMRec
```

### Uninstall

```powershell
.\scripts\windows\uninstall_service.ps1
```

---

## 4. Frontend Production Build

### Option A — Embedded in FastAPI (recommended for single-server)

```bash
cd frontend
npm install
# Set the API base URL to your server address:
VITE_API_BASE_URL=http://192.168.1.10:8000 npm run build
# Or for localhost-only:
# VITE_API_BASE_URL=http://localhost:8000 npm run build
```

When `frontend/dist/` exists next to the repo, FastAPI serves the SPA at `/`.
The API stays at `/api/v1/`.

### Option B — Nginx reverse proxy (optional, LAN)

Use `docs/nginx.conf.example` for HTTP or HTTPS with a self-signed cert:

```bash
sudo cp docs/nginx.conf.example /etc/nginx/conf.d/pgmrec.conf
# Edit server_name to your LAN IP or hostname
sudo nginx -t && sudo systemctl reload nginx
```

Start the backend with `--host 0.0.0.0` so Nginx can reach it, and set:

```env
PGMREC_CORS_ORIGINS=http://pgmrec.local
```

HTTPS on the LAN is optional — generate a self-signed cert or use a local CA
(see the commented section in `docs/nginx.conf.example`).

---

## 5. Configuration

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `PGMREC_JWT_SECRET_KEY` | JWT signing secret (**change before use**) | insecure default |
| `PGMREC_ADMIN_USERNAME` | Initial admin username | `admin` |
| `PGMREC_ADMIN_PASSWORD` | Initial admin password (**change before use**) | `pgmrec-admin` |
| `PGMREC_CORS_ORIGINS` | Allowed browser origins (comma-separated) | `http://localhost:8000` |
| `PGMREC_DATA_DIR` | Root data directory | `backend/data` |
| `PGMREC_LOGS_DIR` | Log file directory | `backend/logs` |
| `PGMREC_FFMPEG_PATH_OVERRIDE` | Global FFmpeg binary override | (per-channel) |
| `PGMREC_FFPROBE_PATH` | FFprobe binary | `ffprobe` |
| `PGMREC_DATABASE_URL` | SQLite URL | `sqlite:///backend/pgmrec.db` |
| `PGMREC_MAX_CONCURRENT_EXPORTS` | Parallel export jobs | `2` |
| `PGMREC_EXPORT_RETENTION_DAYS` | Days to keep exports (0=off) | `30` |
| `PGMREC_PREVIEW_DIR` | HLS preview output | `backend/data/preview` |

See `.env.example` for the full list.

---

## 6. Logs

| Path | Contents |
|------|----------|
| `logs/channels/{id}/ffmpeg-*.log` | Per-channel FFmpeg recording stderr |
| `logs/channels/{id}/preview-*.log` | Per-channel HLS preview stderr |
| `logs/exports/{job_id}.log` | Per-export FFmpeg stderr |
| journald (`-u pgmrec`) | Backend service stdout/stderr (systemd) |
| `logs/service-stdout.log` | Backend stdout (Windows NSSM) |

---

## 7. Backup & Restore

```bash
# Backup DB + channel configs + manifests + .env (no large recordings)
python scripts/backup_data.py
# → pgmrec-backup-YYYYMMDD_HHMMSS.zip

# Dry-run restore
python scripts/restore_data.py pgmrec-backup-....zip --dry-run

# Restore (stop PGMRec first)
sudo systemctl stop pgmrec
python scripts/restore_data.py pgmrec-backup-....zip
sudo systemctl start pgmrec
```

---

## 8. Safe Update Procedure

### Docker

```bash
git pull && cd frontend && npm run build && cd ..
docker compose build && docker compose up -d
```

### systemd

```bash
python scripts/backup_data.py        # backup first
git pull
sudo bash scripts/linux/install_systemd.sh   # updates code + deps, preserves .env
sudo systemctl restart pgmrec
```

### Windows service

```powershell
.\scripts\windows\stop_service.ps1
git pull
.\scripts\windows\install_service.ps1       # updates code + deps, preserves .env
.\scripts\windows\start_service.ps1
```

---

## 9. Roles

| Role | Capabilities |
|------|-------------|
| `admin` | Full access — recording, exports, preview start/stop |
| `export` | View recordings, create/view exports, view preview |
| `preview` | Dashboard, channel status, live preview only |

---

## 10. Development Setup

```bash
# Backend (binds 127.0.0.1 by default)
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend && npm install && npm run dev    # http://localhost:3000

# Tests
cd backend && python -m pytest tests/ -q
```
