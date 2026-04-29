"""
PGMRec — Program Recorder System
FastAPI application entry point.

Phase 1.5: recording reliability layer.
  - Recording watchdog (process alive + file output monitoring, auto-restart)
  - File mover (1_record → 2_chunks)
  - Retention cleaner (delete old files from 3_final)
  - Improved process manager (adopt orphaned PIDs, track last_seen_alive)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config.settings import get_settings
from .db.models import Channel
from .db.session import get_session_factory, init_db
from .models.schemas import ChannelConfig
from .services.file_mover import run_file_mover
from .services.process_manager import get_process_manager
from .services.retention import run_retention
from .services.scheduler import get_scheduler
from .services.watchdog import run_watchdog
from .api.v1 import channels as channels_router
from .api.v1 import monitoring as monitoring_router

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

    Safe to run repeatedly — existing records are never overwritten, so operator
    customisations made via the API (future) are preserved.
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
    for directory in (settings.data_dir, settings.logs_dir, settings.channels_config_dir):
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

    # Register and start background scheduler jobs
    scheduler = get_scheduler()
    scheduler.add(
        "watchdog",
        settings.watchdog_interval_seconds,
        run_watchdog,
    )
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
    await scheduler.start()

    logger.info("PGMRec %s ready.", settings.app_version)
    yield

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
            "Phase 1.5: recording reliability layer."
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
