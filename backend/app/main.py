"""
PGMRec — Program Recorder System
FastAPI application entry point.

Phase 2A: Recording Manifest & Export Index Layer.
  - Per-channel daily JSON manifests (data/manifests/{id}/{YYYY-MM-DD}.json)
  - Segment registration triggered by file_mover (ffprobe + gap detection)
  - DB index: SegmentRecord + ManifestGap tables
  - Manifest API: GET /manifests/{date}, GET /segments, POST /exports/resolve-range

Phase 2B: Export Engine.
  - ExportJob table tracks asynchronous export jobs
  - export_service.py: FFmpeg stream-copy + re-encode fallback
  - export_worker.py: async polling worker (configurable concurrency)
  - Export API: POST /channels/{id}/exports, GET /exports/{id}, GET /exports,
                POST /exports/{id}/cancel

Phase 2C: Export Hardening & Verification.
  - Output verification via ffprobe after every export
  - actual_duration_seconds stored in ExportJob
  - Partial output removed on cancel/failure
  - GET /exports/{id}/logs — raw FFmpeg stderr log
  - GET /exports/{id}/download — FileResponse for completed jobs
  - API validation: in_time < out_time, no future dates, max duration
  - export_retention.py: scheduled cleanup of old export files and logs
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config.settings import get_settings
from .db.models import Channel
from .db.session import get_session_factory, init_db
from .models.schemas import ChannelConfig
from .services.export_retention import run_export_retention
from .services.export_worker import get_export_worker
from .services.file_mover import run_file_mover
from .services.preview_manager import run_preview_watchdog_loop
from .services.process_manager import get_process_manager
from .services.retention import run_retention
from .services.scheduler import get_scheduler
from .services.watchdog import run_watchdog_loop
from .api.v1 import channels as channels_router
from .api.v1 import exports as exports_router
from .api.v1 import manifests as manifests_router
from .api.v1 import monitoring as monitoring_router
from .api.v1 import preview as preview_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Channel seeding ──────────────────────────────────────────────────────────

def _seed_channels(db) -> None:
    """
    On first startup, load *.json files from data/channels/ and insert any
    channels not yet in the DB.

    Safe to run repeatedly — existing records are never overwritten.
    """
    settings = get_settings()
    cfg_dir = settings.channels_config_dir
    if not cfg_dir.exists():
        return

    for cfg_file in sorted(cfg_dir.glob("*.json")):
        try:
            config = ChannelConfig.model_validate_json(
                cfg_file.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("Skipping %s — parse error: %s", cfg_file.name, exc)
            continue

        if db.query(Channel).filter(Channel.id == config.id).first():
            logger.debug("Channel '%s' already in DB — skipping seed.", config.id)
            continue

        channel = Channel(
            id=config.id,
            name=config.name,
            display_name=config.display_name,
            enabled=config.enabled,
            config_json=config.model_dump_json(),
        )
        db.add(channel)
        logger.info("Seeded channel: %s (%s)", config.id, config.display_name)

    db.commit()


# ─── Application lifecycle ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Ensure required directories exist
    for directory in (
        settings.data_dir, settings.logs_dir, settings.channels_config_dir,
        settings.manifests_dir, settings.exports_dir, settings.export_logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    # Create DB tables (idempotent — safe on every restart)
    init_db()

    SessionLocal = get_session_factory()

    # Seed channels from JSON config files
    with SessionLocal() as db:
        _seed_channels(db)

    # Reconcile any stale process records from a previous run
    # (adopts live orphaned PIDs; marks dead ones as stopped)
    with SessionLocal() as db:
        get_process_manager().reconcile_on_startup(db)

    # ── Watchdog — independent asyncio Task ───────────────────────────────
    # Runs its own interval loop, completely decoupled from the shared
    # scheduler, so it is never delayed by file_mover or retention work.
    watchdog_task = asyncio.create_task(
        run_watchdog_loop(), name="pgmrec-watchdog"
    )

    # ── Preview watchdog — independent asyncio Task ────────────────────────
    # Light version: only marks DOWN, never auto-restarts, never touches
    # the recording pipeline.
    preview_watchdog_task = asyncio.create_task(
        run_preview_watchdog_loop(), name="pgmrec-preview-watchdog"
    )

    # ── Export worker — independent asyncio Task ───────────────────────────
    # Polls the DB for QUEUED export jobs and runs them with bounded concurrency.
    export_worker = get_export_worker()
    export_worker.start()

    # ── Shared scheduler: file mover + retention ─────────────────────────
    scheduler = get_scheduler()
    scheduler.add(
        "file_mover",
        settings.file_mover_interval_seconds,
        run_file_mover,
    )
    scheduler.add(
        "retention",
        settings.retention_run_interval_seconds,
        run_retention,
    )
    scheduler.add(
        "export_retention",
        settings.retention_run_interval_seconds,
        run_export_retention,
    )
    await scheduler.start()

    logger.info("PGMRec %s ready.", settings.app_version)
    yield

    # Graceful shutdown
    watchdog_task.cancel()
    preview_watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    try:
        await preview_watchdog_task
    except asyncio.CancelledError:
        pass

    await export_worker.stop()
    await scheduler.stop()
    logger.info("PGMRec shutting down.")


# ─── App factory ──────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Broadcast-grade recording and compliance system. "
            "Phase 2C: Export Hardening — output verification, logs/download endpoints, "
            "strong validation, and export retention cleanup."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(channels_router.router, prefix="/api/v1")
    app.include_router(monitoring_router.router, prefix="/api/v1")
    app.include_router(preview_router.router, prefix="/api/v1")
    app.include_router(manifests_router.router, prefix="/api/v1")
    app.include_router(exports_router.router, prefix="/api/v1")

    @app.get("/health", tags=["system"])
    def health():
        """Liveness probe."""
        return {
            "status": "ok",
            "app": settings.app_name,
            "version": settings.app_version,
        }

    return app


app = create_app()
