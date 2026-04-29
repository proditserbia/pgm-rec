# PGMRec — Backend

Broadcast-grade recording control system — Phase 1 backend.

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

## API endpoints (Phase 1)

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/api/v1/channels/` | List all channels with live status |
| `GET` | `/api/v1/channels/{id}` | Channel detail + config + status |
| `GET` | `/api/v1/channels/{id}/status` | Live PID / uptime |
| `POST` | `/api/v1/channels/{id}/start` | Start recording |
| `POST` | `/api/v1/channels/{id}/stop` | Stop recording |
| `POST` | `/api/v1/channels/{id}/restart` | Restart recording |
| `GET` | `/api/v1/channels/{id}/logs?lines=100` | Tail FFmpeg stderr log |
| `GET` | `/api/v1/channels/{id}/command` | Preview FFmpeg command (dry-run) |
| `GET` | `/api/v1/channels/{id}/history?limit=20` | Recent process records |

## Channel configuration

Channel configs live in `data/channels/*.json` and are seeded into the DB
on first startup.  Editing the JSON and restarting the server does **not**
overwrite DB records — delete the DB file to re-seed from JSON.

## FFmpeg command generated for RTS1

Equivalent of `record_rts1.bat`:

```
C:\AutoRec\ffmpeg\bin\ffmpeg.exe
  -f dshow -s 720x576 -framerate 25
  -i video=Decklink Video Capture:audio=Decklink Audio Capture
  -b:v 1500k -b:a 128k
  -vf drawtext=fontsize=13:fontcolor=black:box=1:boxcolor=white@0.4:
       fontfile='C\:\\Windows\\Fonts\\verdana.ttf':
       text='%{localtime\:%d\-%m\-%y %H\:%M\:%S}':x=(w-tw)/30:y=(h-th)/20,
       scale=1024:576,yadif
  -f stream_segment -segment_time 00:05:00
  -segment_atclocktime 1 -reset_timestamps 1 -strftime 1
  -c:v libx264 -preset veryfast
  D:\AutoRec\record\rts1\1_record\%d%m%y-%H%M%S.mp4
```

## Project structure

```
backend/
├── app/
│   ├── main.py                   FastAPI app + lifespan (seed + reconcile)
│   ├── config/settings.py        App settings (env-overridable via PGMREC_*)
│   ├── models/schemas.py         Pydantic channel config + API response models
│   ├── db/
│   │   ├── models.py             SQLAlchemy ORM (channels, process_records)
│   │   └── session.py            Engine, session factory, get_db dependency
│   ├── services/
│   │   ├── ffmpeg_builder.py     FFmpeg arg-list builder (shell=False safe)
│   │   └── process_manager.py   PID-based process lifecycle manager
│   └── api/v1/channels.py       REST routes
├── data/channels/rts1.json       RTS1 channel config (seeded on first run)
├── logs/                         FFmpeg stderr logs (per channel, timestamped)
└── requirements.txt
```

## Original scripts

The original `.bat` scripts in `scripts/` are **not removed or modified**.
They remain the reference implementation and fallback for operators.
