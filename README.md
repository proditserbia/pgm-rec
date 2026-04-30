# PGMRec — Broadcast Recording & Compliance System

PGMRec is a broadcast-grade SDI/HDMI capture and compliance recording system
built on FFmpeg + FastAPI + React.

## Architecture

```
backend/   FastAPI + SQLite backend (uvicorn)
frontend/  React 18 + TypeScript + Vite web UI
scripts/   Deployment helpers (Linux + Windows)
docs/      Configuration examples
```

---

## Deployment

### Recommended layout

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
# Edit .env — change PGMREC_JWT_SECRET_KEY and PGMREC_ADMIN_PASSWORD

# 4. Start
docker compose up -d

# 5. Open browser
# http://localhost:8000
```

#### Data volumes

Docker Compose creates two named volumes (`pgmrec-data`, `pgmrec-logs`).
To use host paths instead, edit `docker-compose.yml`:

```yaml
volumes:
  - /opt/pgmrec/data:/app/data
  - /opt/pgmrec/logs:/app/logs
```

#### Updating

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

### Install

```bash
sudo bash scripts/linux/install_systemd.sh
```

The script creates `/opt/pgmrec/`, a `pgmrec` system user, a Python venv,
installs deps, and registers the `pgmrec.service` systemd unit.

### Configure

```bash
sudo nano /opt/pgmrec/.env
# Set PGMREC_JWT_SECRET_KEY and PGMREC_ADMIN_PASSWORD
```

### Control

```bash
sudo systemctl start pgmrec
sudo systemctl stop pgmrec
sudo systemctl status pgmrec
sudo journalctl -u pgmrec -f        # live logs

# Convenience wrappers (run from repo root):
bash scripts/linux/start.sh
bash scripts/linux/stop.sh
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

> **DeckLink on Windows** — install the Blackmagic Design DeckLink driver.
> Point `PGMREC_FFMPEG_PATH_OVERRIDE` to an FFmpeg build compiled with `decklink`.

### Install

```powershell
Set-ExecutionPolicy Bypass -Scope Process
.\scripts\windows\install_service.ps1
```

NSSM is downloaded automatically and registers `PGMRec` as a Windows service
that starts automatically on boot.

### Configure

```
notepad C:\PGMRec\.env
```

Key Windows settings:

```env
PGMREC_FFMPEG_PATH_OVERRIDE=C:\AutoRec\ffmpeg\bin\ffmpeg.exe
PGMREC_FFPROBE_PATH=C:\AutoRec\ffmpeg\bin\ffprobe.exe
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
VITE_API_BASE_URL=https://rec.example.com npm run build
```

When `frontend/dist/` exists next to the repo, FastAPI serves the SPA at `/`.
The API stays at `/api/v1/`.

### Option B — Nginx reverse proxy

Use `docs/nginx.conf.example`:

```bash
sudo cp docs/nginx.conf.example /etc/nginx/conf.d/pgmrec.conf
# Edit server_name and SSL paths
sudo nginx -t && sudo systemctl reload nginx
```

Set `PGMREC_CORS_ORIGINS=https://rec.example.com` in `.env`.

---

## 5. Configuration

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `PGMREC_JWT_SECRET_KEY` | JWT signing secret (**change in production**) | insecure default |
| `PGMREC_ADMIN_USERNAME` | Initial admin username | `admin` |
| `PGMREC_ADMIN_PASSWORD` | Initial admin password (**change in production**) | `pgmrec-admin` |
| `PGMREC_CORS_ORIGINS` | Allowed CORS origins (comma-separated; `*` for dev) | `*` |
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
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd frontend && npm install && npm run dev    # http://localhost:3000

# Tests
cd backend && python -m pytest tests/ -q
```
