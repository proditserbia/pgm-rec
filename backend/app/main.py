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
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config.settings import get_settings
from .db.models import Channel, ExportJob, User
from .db.session import get_session_factory, init_db
from .models.schemas import ChannelConfig
from .services.auth_service import create_user, get_user_by_username
from .services.export_retention import run_export_retention
from .services.export_worker import get_export_worker
from .services.file_mover import run_file_mover
from .services.preview_manager import run_preview_watchdog_loop as _mjpeg_watchdog_loop
from .services.hls_preview_manager import run_hls_preview_watchdog_loop
from .services.process_manager import get_process_manager
from .services.retention import run_retention
from .services.scheduler import get_scheduler
from .services.watchdog import run_watchdog_loop
from .api.v1 import channels as channels_router
from .api.v1 import exports as exports_router
from .api.v1 import manifests as manifests_router
from .api.v1 import monitoring as monitoring_router
from .api.v1 import preview as preview_router
from .api.v1 import auth as auth_router

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


def _seed_admin(db) -> None:
    """
    Phase 4 — create the default admin account on first startup.

    Only runs if no users exist in the DB yet.  Credentials come from env vars
    PGMREC_ADMIN_USERNAME / PGMREC_ADMIN_PASSWORD (defaults are insecure —
    always override in production).
    """
    from .db.models import User  # local import avoids circular at module level
    if db.query(User).count() > 0:
        return  # users already seeded

    settings = get_settings()
    create_user(db, settings.admin_username, settings.admin_password, "admin")
    logger.info(
        "Seeded default admin user '%s'. "
        "⚠️  Change PGMREC_ADMIN_PASSWORD before going to production!",
        settings.admin_username,
    )


def _warn_default_credentials() -> None:
    """
    Phase 6.2 — emit CRITICAL log messages if default credentials are still in use.
    Helps catch misconfigured production deployments before they become a problem.
    """
    settings = get_settings()
    _DEFAULT_JWT = "change-me-in-production-pgmrec-secret"
    _DEFAULT_PW = "pgmrec-admin"
    _DEFAULT_USER = "admin"

    if settings.jwt_secret_key == _DEFAULT_JWT:
        logger.critical(
            "⚠️  SECURITY: PGMREC_JWT_SECRET_KEY is set to the default value. "
            "Generate a strong secret with: python -c \"import secrets; print(secrets.token_hex(32))\" "
            "and set it in your .env before serving real traffic."
        )
    if settings.admin_password == _DEFAULT_PW:
        logger.critical(
            "⚠️  SECURITY: PGMREC_ADMIN_PASSWORD is set to the default 'pgmrec-admin'. "
            "Change it before going to production."
        )
    if settings.admin_username == _DEFAULT_USER:
        logger.warning(
            "⚠️  SECURITY: PGMREC_ADMIN_USERNAME is still 'admin'. "
            "Consider using a less predictable username in production."
        )


def _warn_multiple_workers() -> None:
    """
    Phase 6.2 — detect if multiple uvicorn workers are running.

    PGMRec uses process-level singletons (ProcessManager, HlsPreviewManager,
    ExportWorker).  Running with --workers > 1 would create separate instances
    per worker that fight over the same FFmpeg processes and DB rows.
    """
    try:
        import multiprocessing
        # Check UVICORN_WORKERS env var (set by some deployments)
        workers_env = os.environ.get("UVICORN_WORKERS", "")
        if workers_env and workers_env.strip() not in ("", "1"):
            logger.critical(
                "⚠️  WORKERS=%s detected via UVICORN_WORKERS. "
                "PGMRec MUST run with a single worker (--workers 1). "
                "Multi-worker deployments will cause undefined behaviour.",
                workers_env,
            )
            return

        # Check WEB_CONCURRENCY (used by gunicorn / some frameworks)
        concurrency = os.environ.get("WEB_CONCURRENCY", "")
        if concurrency and concurrency.strip() not in ("", "1"):
            logger.critical(
                "⚠️  WEB_CONCURRENCY=%s detected. "
                "PGMRec MUST run with a single worker. "
                "Multi-worker deployments will cause undefined behaviour.",
                concurrency,
            )
    except Exception:
        pass  # never let this crash startup


def _reconcile_stale_exports(db) -> None:
    """
    Phase 6.2 — mark any IN_PROGRESS / RUNNING export jobs as FAILED on startup.

    If the server crashed or was force-killed while an export was running,
    those jobs are stuck in 'running' status forever.  This pass detects and
    resets them so the queue can proceed.
    """
    stale_statuses = ("running", "in_progress")
    stale = (
        db.query(ExportJob)
        .filter(ExportJob.status.in_(stale_statuses))
        .all()
    )
    if not stale:
        return
    for job in stale:
        job.status = "failed"
        job.error_message = "Server restarted while export was in progress — please re-run."
    db.commit()
    logger.warning(
        "Reset %d stale export job(s) from in-progress → failed after server restart.",
        len(stale),
    )


# ─── Application lifecycle ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Phase 6.2 — pre-flight safety checks (log before DB/services start)
    _warn_multiple_workers()
    _warn_default_credentials()

    # Ensure required directories exist
    for directory in (
        settings.data_dir, settings.logs_dir, settings.channels_config_dir,
        settings.manifests_dir, settings.exports_dir, settings.export_logs_dir,
        settings.preview_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    # Create DB tables (idempotent — safe on every restart for SQLite dev/test).
    # For PostgreSQL production, tables are managed by Alembic migrations.
    init_db()

    SessionLocal = get_session_factory()

    # Seed channels from JSON config files
    with SessionLocal() as db:
        _seed_channels(db)

    # Seed default admin user (Phase 4)
    with SessionLocal() as db:
        _seed_admin(db)

    # Phase 6.2 — Reset any export jobs that were stuck in-progress after a crash
    with SessionLocal() as db:
        _reconcile_stale_exports(db)

    # Phase 6.2 — Load restart history from DB into in-memory backoff counters
    with SessionLocal() as db:
        get_process_manager().load_restart_history_from_db(db)

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

    # ── HLS Preview watchdog — independent asyncio Task ────────────────────
    # Light version: only marks DOWN, never auto-restarts, never touches
    # the recording pipeline.
    preview_watchdog_task = asyncio.create_task(
        run_hls_preview_watchdog_loop(), name="pgmrec-hls-preview-watchdog"
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

    # Parse CORS origins from comma-separated string
    _cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router.router, prefix="/api/v1")
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

    # ── Serve pre-built React frontend (Phase 6) ─────────────────────────
    # If frontend/dist exists (production build), serve it as static files.
    # The SPA catch-all serves index.html for all non-API routes so that
    # client-side routing works when navigating directly to a URL.
    _frontend_dist = Path(__file__).parent.parent.parent.parent / "frontend" / "dist"
    if _frontend_dist.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_frontend_dist / "assets")),
            name="frontend-assets",
        )

        from fastapi.responses import FileResponse as _FileResponse

        @app.get("/{full_path:path}", include_in_schema=False)
        def _spa_fallback(full_path: str):
            """Serve index.html for all non-API SPA routes."""
            index = _frontend_dist / "index.html"
            return _FileResponse(str(index))

    return app


app = create_app()
