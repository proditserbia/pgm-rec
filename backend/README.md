# PGMRec — Backend

Broadcast-grade recording control system — Phase 2C: Export Hardening & Verification.

## Requirements

- Python 3.11+
- FFmpeg (path configured per channel)
- Windows (primary target) or Linux

## Setup

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

API docs available at <http://localhost:8000/docs>

## Environment variables

All variables are prefixed with `PGMREC_`.

| Variable | Default | Description |
|---|---|---|
| `PGMREC_DATABASE_URL` | `sqlite:///./pgmrec.db` | SQLAlchemy DB URL |
| `PGMREC_LOGS_DIR` | `backend/logs` | FFmpeg log output directory |
| `PGMREC_STOP_TIMEOUT_SECONDS` | `15` | Seconds to wait for SIGTERM before SIGKILL |
| `PGMREC_DEBUG` | `false` | Enable debug logging |
| `PGMREC_MANIFESTS_DIR` | `backend/data/manifests` | Per-channel daily JSON manifests |
| `PGMREC_MANIFEST_TIMEZONE` | `Europe/Belgrade` | IANA timezone for segment filenames |
| `PGMREC_MANIFEST_GAP_TOLERANCE_SECONDS` | `10.0` | Gaps smaller than this are ignored |
| `PGMREC_EXPORTS_DIR` | `backend/data/exports` | Exported video file output directory |
| `PGMREC_EXPORT_LOGS_DIR` | `backend/logs/exports` | Per-job FFmpeg export logs |
| `PGMREC_MAX_CONCURRENT_EXPORTS` | `2` | Maximum simultaneous export jobs |
| `PGMREC_EXPORT_FFMPEG_THREADS` | `0` | FFmpeg threads per export (0 = auto) |
| `PGMREC_EXPORT_RETENTION_DAYS` | `30` | Delete export files/logs older than N days (0 = disabled) |
| `PGMREC_MAX_EXPORT_DURATION_SECONDS` | `7200` | Reject export requests longer than N seconds (0 = unlimited) |
| `PGMREC_EXPORT_DURATION_TOLERANCE_SECONDS` | `5.0` | Acceptable gap between requested and verified duration |

## API endpoints

### System

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |

### Channels (Phase 1)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/channels/` | List all channels with live status |
| `GET` | `/api/v1/channels/{id}` | Channel detail + config + status |
| `GET` | `/api/v1/channels/{id}/status` | Live PID / uptime |
| `POST` | `/api/v1/channels/{id}/start` | Start recording |
| `POST` | `/api/v1/channels/{id}/stop` | Stop recording |
| `POST` | `/api/v1/channels/{id}/restart` | Restart recording |
| `GET` | `/api/v1/channels/{id}/logs?lines=100` | Tail FFmpeg stderr log |
| `GET` | `/api/v1/channels/{id}/command` | Preview FFmpeg command (dry-run) |
| `GET` | `/api/v1/channels/{id}/history?limit=20` | Recent process records |

### Manifest & Export Index (Phase 2A)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/channels/{id}/manifests/{date}` | Daily JSON manifest for a channel |
| `GET` | `/api/v1/channels/{id}/segments?date=YYYY-MM-DD` | Segment list from DB for a date |
| `POST` | `/api/v1/channels/{id}/exports/resolve-range` | Resolve export range (no video yet) |

### Export Engine (Phase 2B + 2C)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/channels/{id}/exports` | Create and queue a new export job |
| `GET` | `/api/v1/exports/{job_id}` | Get export job status / progress |
| `GET` | `/api/v1/exports?channel_id=&status=&limit=` | List export jobs |
| `POST` | `/api/v1/exports/{job_id}/cancel` | Cancel a queued or running job |
| `GET` | `/api/v1/exports/{job_id}/logs` | Raw FFmpeg stderr log for a job (Phase 2C) |
| `GET` | `/api/v1/exports/{job_id}/download` | Download completed export file (Phase 2C) |

## Export flow (Phase 2B + 2C)

```
POST /channels/rts1/exports  {"date":"2026-04-01","in_time":"14:05:30","out_time":"14:22:10"}
  │
  ├─ Phase 2C API validation:
  │    • in_time must be strictly before out_time
  │    • date must not be in the future
  │    • duration ≤ max_export_duration_seconds (when > 0)
  ├─ validate range via resolve_export_range() (Phase 2A manifest/DB)
  ├─ create ExportJob (status=queued) in DB
  ├─ wake export_worker
  │
  └─ ExportWorker (background asyncio task)
       │
       ├─ acquire concurrency semaphore (max_concurrent_exports)
       ├─ run_export_job(job_id)
       │    ├─ resolve range → segment list + first_segment_offset
       │    ├─ check all segment files exist on disk
       │    ├─ build output path: data/exports/{channel}/{date}/{name}.mp4
       │    ├─ strategy A — single segment:
       │    │     ffmpeg -ss <offset> -i <file> -t <duration> -c copy output.mp4
       │    ├─ strategy B — multi-segment (concat demuxer):
       │    │     write ffconcat file with inpoint/outpoint directives
       │    │     ffmpeg -f concat -safe 0 -i concat.txt -c copy output.mp4
       │    ├─ fallback — if stream copy fails:
       │    │     ffmpeg ... -c:v libx264 -preset veryfast -c:a aac output.mp4
       │    ├─ capture stderr → logs/exports/{channel}/{date}/export_{id}.log
       │    ├─ update progress_percent from FFmpeg time= lines
       │    │
       │    ├─ Phase 2C — output verification:
       │    │     • file exists and size > 0
       │    │     • ffprobe reads actual duration
       │    │     • actual duration within tolerance of requested duration
       │    │     • store actual_duration_seconds in ExportJob
       │    │     • if any check fails → mark job failed
       │    │
       │    └─ on cancel/fail → remove partial output file
       │
       └─ mark job completed / failed / cancelled in DB

Export retention (Phase 2C — runs hourly):
  • delete *.mp4 under exports_dir older than export_retention_days
  • delete export_*.log under export_logs_dir older than export_retention_days
  • prune now-empty date subdirectories
  • disabled when export_retention_days = 0
```

## Channel configuration

Channel configs live in `data/channels/*.json` and are seeded into the DB
on first startup.  Editing the JSON and restarting the server does **not**
overwrite DB records — delete the DB file to re-seed from JSON.

## Project structure

```
backend/
├── app/
│   ├── main.py                    FastAPI app + lifespan
│   ├── config/settings.py         App settings (env-overridable via PGMREC_*)
│   ├── models/schemas.py          Pydantic config + API response models
│   ├── db/
│   │   ├── models.py              SQLAlchemy ORM (all tables)
│   │   └── session.py             Engine, session factory, get_db dependency
│   ├── services/
│   │   ├── ffmpeg_builder.py      FFmpeg arg-list builder (shell=False safe)
│   │   ├── process_manager.py     PID-based process lifecycle manager
│   │   ├── watchdog.py            File/process health watchdog
│   │   ├── file_mover.py          Moves completed segments 1_record → 2_chunks
│   │   ├── retention.py           Age-based file retention cleanup
│   │   ├── scheduler.py           Interval task scheduler
│   │   ├── preview_manager.py     Preview stream manager (Phase 2)
│   │   ├── manifest_service.py    Manifest write/read, register_segment (Phase 2A)
│   │   ├── export_service.py      FFmpeg export logic + output verification (Phase 2B/2C)
│   │   ├── export_worker.py       Async export job worker (Phase 2B)
│   │   └── export_retention.py    Export file/log cleanup (Phase 2C)
│   └── api/v1/
│       ├── channels.py            Recording control endpoints
│       ├── monitoring.py          Health / watchdog endpoints
│       ├── preview.py             Preview stream endpoints
│       ├── manifests.py           Manifest + range-resolver endpoints (Phase 2A)
│       └── exports.py             Export job CRUD + logs/download endpoints (Phase 2B/2C)
├── data/
│   ├── channels/*.json            Channel configs (seeded on first run)
│   ├── manifests/{ch}/{date}.json Daily recording manifests (Phase 2A)
│   └── exports/{ch}/{date}/*.mp4  Exported video files (Phase 2B)
├── logs/
│   ├── channels/{id}/             FFmpeg recording logs
│   └── exports/{ch}/{date}/       Per-job FFmpeg export logs (Phase 2B)
└── requirements.txt
```

## Original scripts

The original `.bat` scripts in `scripts/` are **not removed or modified**.
They remain the reference implementation and fallback for operators.
