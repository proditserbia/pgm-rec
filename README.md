# PGMRec — Broadcast Recording & Compliance System

PGMRec is a broadcast-grade SDI/HDMI capture and compliance recording system
built on FFmpeg + FastAPI + React.

---

> ## ⚠️ LAN-only deployment
>
> PGMRec is designed exclusively for **internal broadcast/server networks**.
> It is **not** intended to be exposed to the public internet.
>
> - No public SaaS or cloud hosting
> - No external service dependencies
> - All features work fully **offline** on a LAN after installation
> - Runtime requires **no internet connectivity**
>
> **Security requirements still apply even on a LAN:**
> - Authentication and role-based access are mandatory
> - Set a strong `PGMREC_ADMIN_PASSWORD` before first use
> - Set a strong `PGMREC_JWT_SECRET_KEY` before first use
> - Keep CORS limited to your actual UI origin(s)
> - Do not open port 8000 to the internet

---

## Architecture

```
backend/   FastAPI + PostgreSQL/SQLite backend (uvicorn)
frontend/  React 18 + TypeScript + Vite web UI (all assets bundled locally — no CDN)
scripts/   Deployment helpers (Linux + Windows)
docs/      Configuration examples
```

---

## Database — SQLite vs PostgreSQL

| | SQLite | PostgreSQL |
|---|---|---|
| **Recommended for** | Development, testing, single-channel trials | **Production, 24/7 multi-channel recording** |
| **Setup** | Zero — file is created automatically | Requires PostgreSQL server |
| **Concurrency** | Limited (WAL mode enabled automatically) | Full MVCC — safe for concurrent reads/writes |
| **Stability under load** | Can block under sustained write pressure | Handles continuous write load without issue |
| **Data persistence** | Single `.db` file | Managed server with backup tools |

**For any 24/7 production deployment, PostgreSQL is strongly recommended.**

### Using PostgreSQL

1. Install PostgreSQL 15+ on your server (or use the Docker Compose setup which includes it automatically).

2. Create the database and user:
   ```sql
   CREATE USER pgmrec WITH PASSWORD 'your-strong-password';
   CREATE DATABASE pgmrec OWNER pgmrec;
   ```

3. Set the database URL in `.env`:
   ```env
   PGMREC_DATABASE_URL=postgresql+psycopg://pgmrec:your-strong-password@localhost:5432/pgmrec
   ```

4. Run database migrations (from `backend/` directory):
   ```bash
   cd backend
   alembic upgrade head
   ```

5. Start PGMRec normally.  The app reads `PGMREC_DATABASE_URL` automatically.

### Running Alembic migrations

```bash
cd backend

# Apply all pending migrations (always run after git pull on PostgreSQL)
alembic upgrade head

# Show current migration state
alembic current

# Show migration history
alembic history

# Roll back one migration
alembic downgrade -1
```

> **Note for SQLite dev/test:** Tables are created automatically by SQLAlchemy
> on first startup.  Alembic is only required for PostgreSQL production.
>
> If you already have an existing SQLite database and want to bring it under
> Alembic management (so future migrations apply cleanly), run:
> ```bash
> alembic stamp head
> ```
> This tells Alembic "the database is already at the latest migration" without
> re-running the initial migration.

---

## Network deployment model

| Mode | Host | Who can reach it |
|------|------|-----------------|
| **Single-machine** (default) | `127.0.0.1` | Only the local machine |
| **LAN access** | `0.0.0.0` | All machines on the same network segment |
| **With Nginx/Caddy** (optional) | `0.0.0.0` via proxy | LAN machines via hostname; optional HTTPS |

### CORS origins

The default CORS configuration allows both the Vite dev server (port 5173) and
the embedded production UI (port 8000) from localhost:

```
http://localhost:5173
http://127.0.0.1:5173
http://localhost:8000
http://127.0.0.1:8000
```

For **LAN access**, add your server's LAN IP to `PGMREC_CORS_ORIGINS`:

```env
PGMREC_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.50:8000
```

---

## 1. Linux — Docker (recommended)

### Prerequisites

- Docker ≥ 24 and Docker Compose ≥ 2
- Internet access is only needed **once** during `docker build`
- The running container has **no internet dependency**

### Single-machine mode

```bash
# 1. Clone repository
git clone https://github.com/proditserbia/pgm-rec.git
cd pgm-rec

# 2. (Optional) Build frontend so the UI is embedded in the container
cd frontend && npm install && npm run build && cd ..

# 3. Configure
cp .env.example .env
# Required — edit .env:
#   PGMREC_JWT_SECRET_KEY=<generate: python -c "import secrets; print(secrets.token_hex(32))">
#   PGMREC_ADMIN_PASSWORD=<strong password>

# 4. Start (includes PostgreSQL 15 — no separate setup needed)
docker compose up -d

# 5. Apply database migrations (first time only; safe to re-run)
docker compose exec backend alembic upgrade head

# 6. Access (this machine only)
# http://localhost:8000
```

### LAN access mode

```bash
# In .env — add your server's LAN IP to CORS origins:
PGMREC_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.50:8000

# In docker-compose.yml — change ports to listen on all interfaces:
#   - "0.0.0.0:8000:8000"

docker compose up -d
# Access from LAN: http://192.168.1.50:8000  (use your server's actual IP)
```

### Data volumes

By default, data is stored in named Docker volumes (`pgmrec-data`, `pgmrec-logs`).
To use host paths:

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

> **Blackmagic DeckLink** — install the driver from
> https://www.blackmagicdesign.com/support  
> The `decklink` FFmpeg input device must be compiled into your FFmpeg build.
> No internet access is needed after driver installation.

### Install

```bash
sudo bash scripts/linux/install_systemd.sh
```

The script creates `/opt/pgmrec/`, a `pgmrec` system user, a Python venv,
installs dependencies, and registers the `pgmrec.service` systemd unit.

### Configure

```bash
sudo nano /opt/pgmrec/.env
```

Minimum required:

```env
PGMREC_HOST=127.0.0.1              # single-machine mode (default)
# PGMREC_HOST=0.0.0.0             # LAN access mode
PGMREC_PORT=8000
PGMREC_JWT_SECRET_KEY=<strong random string>
PGMREC_ADMIN_PASSWORD=<strong password>
PGMREC_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000
# LAN: add http://192.168.1.50:8000 to the list above
```

### Control

```bash
sudo systemctl start pgmrec
sudo systemctl stop pgmrec
sudo systemctl status pgmrec
sudo journalctl -u pgmrec -f        # live logs

# Convenience wrappers (reads PGMREC_HOST/PORT from .env):
bash scripts/linux/start.sh
bash scripts/linux/stop.sh
```

### LAN access (systemd)

1. In `/opt/pgmrec/.env`:
   ```env
   PGMREC_HOST=0.0.0.0
   PGMREC_CORS_ORIGINS=...,http://192.168.1.50:8000
   ```
2. Edit `/etc/systemd/system/pgmrec.service` — change `--host 127.0.0.1` to `--host 0.0.0.0`
3. Reload:
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
    (download from https://nssm.cc on any internet-connected machine)

> **DeckLink on Windows** — install the Blackmagic Design DeckLink driver and
> point `PGMREC_FFMPEG_PATH_OVERRIDE` to an FFmpeg build compiled with `decklink`.

### Install

```powershell
# Run PowerShell as Administrator from repository root

# Single-machine mode (default — binds 127.0.0.1)
Set-ExecutionPolicy Bypass -Scope Process
.\scripts\windows\install_service.ps1

# LAN access mode (binds 0.0.0.0)
.\scripts\windows\install_service.ps1 -Host 0.0.0.0
```

### Configure

```
notepad C:\PGMRec\.env
```

Minimum required:

```env
PGMREC_HOST=127.0.0.1
PGMREC_PORT=8000
PGMREC_JWT_SECRET_KEY=<strong random string>
PGMREC_ADMIN_PASSWORD=<strong password>
PGMREC_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000

# FFmpeg (adjust to your install location)
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

## 4. Frontend build

All frontend assets are bundled locally by Vite — no CDN, no external resources.
The production build works completely offline.

### Option A — Embedded in FastAPI (recommended for single-server)

```bash
cd frontend
npm install
# Point the API base URL at your server:
VITE_API_BASE_URL=http://localhost:8000 npm run build
# Or for LAN IP:
# VITE_API_BASE_URL=http://192.168.1.50:8000 npm run build
```

When `frontend/dist/` exists, FastAPI serves the SPA at `/` and the API stays at `/api/v1/`.

### Option B — Nginx/Caddy reverse proxy (optional, LAN)

Use `docs/nginx.conf.example`:

```bash
sudo cp docs/nginx.conf.example /etc/nginx/conf.d/pgmrec.conf
# Edit server_name to your LAN IP or hostname
sudo nginx -t && sudo systemctl reload nginx
```

Start the backend with `PGMREC_HOST=0.0.0.0` (so Nginx can proxy to it), then set:

```env
PGMREC_CORS_ORIGINS=http://pgmrec.local
```

### Optional HTTPS on LAN

HTTPS is not required for LAN MVP, but can be added with a self-signed or
internal-CA certificate (see commented section in `docs/nginx.conf.example`):

```bash
# Generate a self-signed cert (valid 10 years)
openssl req -x509 -newkey rsa:4096 -days 3650 -nodes \
  -keyout /etc/ssl/pgmrec/pgmrec.key \
  -out    /etc/ssl/pgmrec/pgmrec.crt \
  -subj "/CN=pgmrec.local"
```

Or use `mkcert` / Smallstep for a browser-trusted local CA certificate.

---

## 5. Configuration reference

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `PGMREC_HOST` | Bind address (`127.0.0.1` or `0.0.0.0`) | `127.0.0.1` |
| `PGMREC_PORT` | Listen port | `8000` |
| `PGMREC_JWT_SECRET_KEY` | JWT signing secret (**change before use**) | insecure default |
| `PGMREC_ADMIN_USERNAME` | Initial admin username | `admin` |
| `PGMREC_ADMIN_PASSWORD` | Initial admin password (**change before use**) | `pgmrec-admin` |
| `PGMREC_CORS_ORIGINS` | Allowed browser origins (comma-separated) | localhost:5173 + localhost:8000 |
| `PGMREC_DATA_DIR` | Root data directory | `backend/data` |
| `PGMREC_LOGS_DIR` | Log file directory | `backend/logs` |
| `PGMREC_RECORDING_ROOT` | Base dir for relative channel recording paths | (not set) |
| `PGMREC_FFMPEG_PATH_OVERRIDE` | Global FFmpeg binary override | (per-channel) |
| `PGMREC_FFPROBE_PATH` | FFprobe binary | `ffprobe` |
| `PGMREC_DATABASE_URL` | SQLite or PostgreSQL URL | `sqlite:///backend/pgmrec.db` |
| `PGMREC_MAX_CONCURRENT_EXPORTS` | Parallel export jobs | `2` |
| `PGMREC_EXPORT_RETENTION_DAYS` | Days to keep exports (0=off) | `30` |
| `PGMREC_MIN_FREE_DISK_BYTES` | Min free disk before recording starts | `524288000` (500 MB) |
| `PGMREC_EVENT_RETENTION_DAYS` | Days to keep watchdog/anomaly rows | `90` |

See `.env.example` for the complete annotated list.

---

## 5.1 Recording paths: `.env` vs channel JSON

PGMRec uses **two separate configuration layers** for paths.  Understanding
the distinction prevents confusion when moving recordings to a different disk
or directory.

### Global paths — `.env` controls these

| Variable | What it controls |
|----------|-----------------|
| `PGMREC_DATA_DIR` | Root for app data (manifests, exports, preview, channel configs) |
| `PGMREC_LOGS_DIR` | Root for all log files |
| `PGMREC_EXPORTS_DIR` | Where exported video files are written |
| `PGMREC_PREVIEW_DIR` | Where HLS preview segments are written |
| `PGMREC_DATABASE_URL` | Database location (SQLite file or PostgreSQL URL) |

These settings affect the **backend infrastructure** — they do **not**
override per-channel recording directories.

### Per-channel recording paths — channel JSON controls these

Each channel has its own JSON file in `data/channels/` (e.g. `rts1.json`).
The `paths` block defines the **three-stage recording pipeline**:

```json
"paths": {
  "record_dir": "D:\\AutoRec\\record\\rts1\\1_record",
  "chunks_dir": "D:\\AutoRec\\record\\rts1\\2_chunks",
  "final_dir":  "D:\\AutoRec\\record\\rts1\\3_final"
}
```

| Stage | Directory key | Purpose |
|-------|--------------|---------|
| 1 | `record_dir` | FFmpeg writes active segments here |
| 2 | `chunks_dir` | Completed segments are moved here by the file-mover |
| 3 | `final_dir`  | Merged daily files stored here; subject to retention cleanup |

### How to change recording destination

**Option A — Absolute paths in channel JSON (recommended for existing installs)**

Edit the channel JSON and set absolute paths for the new location:

```json
"paths": {
  "record_dir": "E:\\NewDisk\\rts1\\1_record",
  "chunks_dir": "E:\\NewDisk\\rts1\\2_chunks",
  "final_dir":  "E:\\NewDisk\\rts1\\3_final"
}
```

Restart PGMRec for the change to take effect.

**Option B — Relative paths with `PGMREC_RECORDING_ROOT` (recommended for new installs)**

Set the recording root once in `.env`:

```env
PGMREC_RECORDING_ROOT=D:\AutoRec\record     # Windows
# PGMREC_RECORDING_ROOT=/mnt/recordings     # Linux
```

Then use relative paths in every channel JSON:

```json
"paths": {
  "record_dir": "rts1/1_record",
  "chunks_dir": "rts1/2_chunks",
  "final_dir":  "rts1/3_final"
}
```

PGMRec resolves these under `PGMREC_RECORDING_ROOT` at runtime.
To move recordings to a new disk, change only `PGMREC_RECORDING_ROOT` — no
per-channel JSON edits needed.

> **Note:** `.env` settings do **not** retroactively override absolute paths
> that are already set in channel JSON.  If a `paths` value is absolute, it
> is always used exactly as written regardless of `PGMREC_RECORDING_ROOT`.

---

## 6. Roles

| Role | Capabilities |
|------|-------------|
| `admin` | Full access — recording, exports, preview start/stop |
| `export` | View recordings, create/view exports, view preview |
| `preview` | Dashboard, channel status, live preview only |

---

## 7. Logs

| Path | Contents |
|------|----------|
| `logs/channels/{id}/ffmpeg-*.log` | Per-channel FFmpeg recording stderr |
| `logs/channels/{id}/preview-*.log` | Per-channel HLS preview stderr |
| `logs/exports/{job_id}.log` | Per-export FFmpeg stderr |
| journald (`-u pgmrec`) | Backend service stdout/stderr (systemd) |
| `logs/service-stdout.log` | Backend stdout (Windows NSSM) |

---

## 8. Backup & Restore

### SQLite

```bash
# Backup DB + channel configs + manifests + .env (excludes large recording files)
python scripts/backup_data.py
# → pgmrec-backup-YYYYMMDD_HHMMSS.zip

# Dry-run restore
python scripts/restore_data.py pgmrec-backup-....zip --dry-run

# Restore (stop PGMRec first)
sudo systemctl stop pgmrec
python scripts/restore_data.py pgmrec-backup-....zip
sudo systemctl start pgmrec
```

### PostgreSQL

```bash
# Dump the database (run on the server or via Docker):
pg_dump -U pgmrec -h localhost pgmrec > pgmrec-$(date +%Y%m%d_%H%M%S).sql

# Docker Compose backup:
docker exec pgmrec-postgres pg_dump -U pgmrec pgmrec > pgmrec-backup.sql

# Restore:
psql -U pgmrec -h localhost pgmrec < pgmrec-backup.sql

# Docker Compose restore:
docker exec -i pgmrec-postgres psql -U pgmrec pgmrec < pgmrec-backup.sql
```

---

## 9. Development setup

```bash
# Backend (binds 127.0.0.1 by default)
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Frontend dev server (separate terminal)
cd frontend && npm install && npm run dev
# Dev server: http://localhost:5173  (proxies API to localhost:8000)

# Tests
cd backend && python -m pytest tests/ -q
```

---

## 10. Safe update procedure

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

## Troubleshooting

### Black video with Blackmagic Decklink / dshow capture

When recording produces video chunks but the content is black even though an
SDI signal is present, work through the following checklist.

**Step 1 — Verify exact device names**

On the recording machine, open a terminal (or PowerShell) and run:

```
ffmpeg -list_devices true -f dshow -i dummy
```

Copy the exact device names (e.g. `Decklink Video Capture`) and paste them
into the channel JSON config (`capture.video_device` / `capture.audio_device`).
Even a single extra space will cause the device not to be found.

**Step 2 — List supported capture modes**

```
ffmpeg -f dshow -list_options true -i video="Decklink Video Capture"
```

Confirm that the resolution, framerate, and pixel format in your channel
config match a mode listed here.  SDI standard mismatches (PAL/NTSC/HD,
interlaced/progressive, 50 Hz vs 59.94 Hz) are a common cause of black frames
even when signal is present on the physical input.

**Step 3 — Test capture manually**

```
ffmpeg -f dshow -s 720x576 -framerate 25 -i video="Decklink Video Capture":audio="Decklink Audio Capture" -t 10 test.mp4
```

Open `test.mp4` in a media player.  If this also shows black, the issue is
with the capture device, the SDI cable, or the signal standard — not PGMRec.

**Step 4 — Try alternate pixel formats**

Some Decklink cards require an explicit pixel format:

```
ffmpeg -f dshow -pixel_format uyvy422 -s 720x576 -framerate 25 \
       -i video="Decklink Video Capture" -t 10 test.mp4
```

Refer to your Decklink documentation for the supported `pixel_format` values.

**Step 5 — Use the Diagnostics endpoint**

PGMRec provides a live diagnostics endpoint (admin only):

```
GET /api/v1/channels/{id}/diagnostics
```

This returns the exact FFmpeg command, the last 100 lines of recording stderr,
the latest segment path/size, and a device-listing hint.  It is also accessible
from the Channel Detail page in the web UI (collapsible "Channel Diagnostics"
section).

---

### Preview fails with "HLS fatal error: networkError / manifestLoadError"

This error appears in the browser when the HLS playlist is not yet available.

**Cause A — Preview startup in progress**

The preview FFmpeg process takes a few seconds to produce the first `.ts`
segment and write the initial `index.m3u8`.  PGMRec (Phase 9+) will show
"Starting preview…" during this period and will only display the player once
the playlist is ready.  Wait for the status to change to "Running".

**Cause B — Preview FFmpeg failed to open the capture device**

On systems with a **single Blackmagic Decklink input**, the recording process
already owns the device.  The preview process cannot open the same device
concurrently, so it exits immediately and the playlist is never created.

Fix: set `preview.input_mode = "disabled"` in the channel JSON config:

```json
{
  "preview": {
    "input_mode": "disabled"
  }
}
```

With `input_mode = "disabled"`, clicking "Start Preview" returns HTTP 409 with
a clear explanation rather than starting a process that will immediately fail.

To view the preview stderr for a failed attempt, use the "Preview FFmpeg Log
Tail" section in the Channel Detail page (admin only) or call:

```
GET /api/v1/channels/{id}/preview/logs?lines=100
```

---

### `file_mover` logs "Destination already exists"

**Cause — same size**  
The file was already moved to `2_chunks/` in a previous run, but the source
file was not deleted (e.g. due to a crash).  PGMRec (Phase 9+) silently removes
the stale source file instead of logging a warning.

**Cause — different size**  
A genuine name collision: two different files landed with the same name (very
unlikely with strftime naming).  PGMRec renames the existing destination to
`<name>_conflict_<timestamp>.mp4` and then moves the source normally.

If you see repeated warnings, check:
- Are two recording processes writing to the same `1_record/` directory?
- Is the `segment_filename_pattern` unique per channel?
