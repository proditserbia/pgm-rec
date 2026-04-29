"""
Recording Watchdog Service.

Runs on a configurable interval (default 10 s) and checks every active channel:

1. Process alive check
   Is the FFmpeg process still alive?
   If not → log WatchdogEvent(process_dead) + auto-restart.

2. File output check
   Is the 1_record directory receiving new segment files?
   The newest *.mp4 file must be younger than (segment_time + tolerance) seconds.
   If not → log WatchdogEvent(no_new_files) + SegmentAnomaly + auto-restart.

Auto-restart is fire-and-forget: it runs in a thread pool (asyncio.to_thread) so
the blocking process.wait() call never stalls the event loop.

The watchdog also updates ProcessInfo.last_seen_alive / health each cycle so
the status API can expose real-time health information.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config.settings import get_settings
from ..db.models import Channel, SegmentAnomaly, WatchdogEvent
from ..db.session import get_session_factory
from ..models.schemas import ChannelConfig
from .process_manager import get_process_manager

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_segment_seconds(segment_time: str) -> float:
    """Parse HH:MM:SS into total seconds (e.g. '00:05:00' → 300.0)."""
    try:
        parts = segment_time.split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        return float(h * 3600 + m * 60 + s)
    except (ValueError, IndexError):
        return 300.0  # safe default


def _newest_mp4_age(record_dir: Path) -> float | None:
    """
    Return the age in seconds of the newest *.mp4 file in *record_dir*.

    Returns None if the directory doesn't exist or has no mp4 files.
    """
    if not record_dir.exists():
        return None
    try:
        mp4_files = list(record_dir.glob("*.mp4"))
        if not mp4_files:
            return None
        newest_mtime = max(f.stat().st_mtime for f in mp4_files)
        return time.time() - newest_mtime
    except OSError:
        return None


def _log_event(db, channel_id: str, event_type: str, details: str) -> None:
    """Persist a WatchdogEvent row."""
    event = WatchdogEvent(
        channel_id=channel_id,
        event_type=event_type,
        details=details,
    )
    db.add(event)
    db.commit()
    logger.warning("[watchdog][%s] %s — %s", channel_id, event_type, details)


def _log_segment_anomaly(
    db,
    channel_id: str,
    last_segment_time: datetime | None,
    expected_seconds: float,
    actual_gap: float,
) -> None:
    """Persist a SegmentAnomaly row."""
    anomaly = SegmentAnomaly(
        channel_id=channel_id,
        last_segment_time=last_segment_time,
        expected_interval_seconds=expected_seconds,
        actual_gap_seconds=actual_gap,
    )
    db.add(anomaly)
    db.commit()


def _restart_channel_sync(channel_id: str) -> None:
    """
    Synchronous restart helper — safe to run in a thread pool.

    Creates its own DB session so it doesn't share state with the async caller.
    """
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        channel: Channel | None = (
            db.query(Channel).filter(Channel.id == channel_id).first()
        )
        if channel is None:
            logger.error("[watchdog][%s] Channel not found in DB — cannot restart.", channel_id)
            return
        if not channel.enabled:
            logger.info("[watchdog][%s] Channel disabled — skipping restart.", channel_id)
            return
        config = ChannelConfig.model_validate_json(channel.config_json)
        pm = get_process_manager()
        pm.restart(channel_id, config, db)
        # Log the auto-restart event in a fresh transaction
        _log_event(db, channel_id, "auto_restarted", "Watchdog initiated restart")


# ─── Watchdog check ───────────────────────────────────────────────────────────

async def _check_channel(
    channel_id: str,
    config: ChannelConfig,
    uptime_seconds: float,
) -> None:
    """
    Run all health checks for one running channel.

    *uptime_seconds* is the age of the current recording session.
    We skip the file-output check for very new sessions (< segment_time)
    to avoid false positives during the initial buffering phase.
    """
    settings = get_settings()
    pm = get_process_manager()
    SessionLocal = get_session_factory()

    # ── Check 1: process alive ─────────────────────────────────────────────
    pm._reap_if_dead(channel_id)
    if not pm.is_running(channel_id):
        with SessionLocal() as db:
            _log_event(
                db,
                channel_id,
                "process_dead",
                "FFmpeg process exited unexpectedly; triggering auto-restart",
            )
        pm.mark_unhealthy(channel_id)
        logger.info("[watchdog][%s] Scheduling auto-restart (process_dead).", channel_id)
        await asyncio.to_thread(_restart_channel_sync, channel_id)
        return

    # Process is alive — update heartbeat
    pm.mark_alive(channel_id)

    # ── Check 2: file output activity ──────────────────────────────────────
    segment_seconds = _parse_segment_seconds(config.segmentation.segment_time)
    tolerance = float(settings.watchdog_segment_tolerance_seconds)
    max_age = segment_seconds + tolerance

    # Don't check file output until the first segment is expected
    if uptime_seconds < segment_seconds:
        return

    record_dir = Path(config.paths.record_dir)
    file_age = _newest_mp4_age(record_dir)

    if file_age is None:
        # No files yet — could be a very fresh recording but we already
        # confirmed uptime > segment_time so this is suspicious
        with SessionLocal() as db:
            _log_event(
                db,
                channel_id,
                "no_new_files",
                f"No mp4 files found in {record_dir} after {uptime_seconds:.0f}s",
            )
            _log_segment_anomaly(db, channel_id, None, segment_seconds, uptime_seconds)
        pm.mark_unhealthy(channel_id)
        logger.info(
            "[watchdog][%s] No output files found — scheduling auto-restart.", channel_id
        )
        await asyncio.to_thread(_restart_channel_sync, channel_id)
        return

    if file_age > max_age:
        # Files exist but the newest one is too old
        last_seg_ts = datetime.fromtimestamp(
            time.time() - file_age, tz=timezone.utc
        )
        with SessionLocal() as db:
            _log_event(
                db,
                channel_id,
                "no_new_files",
                f"Newest segment is {file_age:.1f}s old (max allowed {max_age:.1f}s)",
            )
            _log_segment_anomaly(db, channel_id, last_seg_ts, segment_seconds, file_age)
        pm.mark_unhealthy(channel_id)
        logger.info(
            "[watchdog][%s] Stale output (age=%.1fs, max=%.1fs) — scheduling restart.",
            channel_id, file_age, max_age,
        )
        await asyncio.to_thread(_restart_channel_sync, channel_id)


# ─── Watchdog loop (scheduled entry point) ────────────────────────────────────

async def run_watchdog() -> None:
    """
    Check all currently running channels.

    Called by the scheduler every watchdog_interval_seconds.
    Iterates over a snapshot of pm._procs to avoid mutation during iteration.
    """
    pm = get_process_manager()
    running = list(pm._procs.items())
    if not running:
        return

    SessionLocal = get_session_factory()

    for channel_id, info in running:
        try:
            # Load config from DB (cheap — config_json is a single TEXT column)
            with SessionLocal() as db:
                channel: Channel | None = (
                    db.query(Channel).filter(Channel.id == channel_id).first()
                )
            if channel is None:
                continue
            config = ChannelConfig.model_validate_json(channel.config_json)

            now = datetime.now(timezone.utc)
            uptime = (now - info.started_at).total_seconds()
            await _check_channel(channel_id, config, uptime)

        except Exception:
            logger.exception("[watchdog][%s] Unexpected error during health check.", channel_id)
