"""
Recording Watchdog Service — Phase 1.5 / 1.6.

Runs as an independent asyncio Task (not via the shared scheduler) so it is
never delayed by file_mover or retention work.

Checks every active channel on each cycle:

1. Process alive check
   Is the FFmpeg process still alive?
   If not → log WatchdogEvent(process_dead) + auto-restart (subject to backoff).

2. File output check (age)
   Is the 1_record directory receiving new segment files?
   The newest *.mp4 must be younger than (segment_time + tolerance) seconds.

3. Stall detection — Phase 1.6
   Is the newest segment file actually GROWING?
   If the file exists but its size has not increased for stall_detection_seconds
   → WatchdogEvent(stalled_output) + auto-restart (subject to backoff).

Restart backoff — Phase 1.6
   All auto-restart attempts go through ProcessManager.attempt_auto_restart().
   If too many restarts happen within restart_backoff_window_seconds, the channel
   enters COOLDOWN and further auto-restarts are blocked until the cooldown
   expires.
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


def _get_newest_mp4(record_dir: Path):
    """
    Return (path, mtime, size) of the newest *.mp4 file in *record_dir*, or
    (None, None, None) if the directory doesn't exist or has no mp4 files.
    """
    if not record_dir.exists():
        return None, None, None
    try:
        mp4_files = list(record_dir.glob("*.mp4"))
        if not mp4_files:
            return None, None, None
        newest = max(mp4_files, key=lambda f: f.stat().st_mtime)
        stat = newest.stat()
        return newest, stat.st_mtime, stat.st_size
    except OSError:
        return None, None, None


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


def _restart_channel_sync(channel_id: str, reason: str) -> None:
    """
    Synchronous restart helper — safe to run in a thread pool.

    All restart attempts go through ProcessManager.attempt_auto_restart() which
    enforces the restart backoff / COOLDOWN policy.

    Creates its own DB session so it doesn't share state with the async caller.
    """
    pm = get_process_manager()

    # Gate through backoff policy
    if not pm.attempt_auto_restart(channel_id):
        # Blocked by cooldown — log the suppression event
        SessionLocal = get_session_factory()
        with SessionLocal() as db:
            _log_event(
                db, channel_id, "restart_suppressed",
                f"Auto-restart blocked by cooldown policy ({reason})"
            )
        return

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
        pm.restart(channel_id, config, db)
        # Log the auto-restart event in a fresh transaction
        _log_event(db, channel_id, "auto_restarted", f"Watchdog initiated restart: {reason}")


# ─── Watchdog check ───────────────────────────────────────────────────────────

async def _check_channel(
    channel_id: str,
    config: ChannelConfig,
    uptime_seconds: float,
) -> None:
    """
    Run all health checks for one running channel.

    *uptime_seconds* is the age of the current recording session.
    The file-output and stall checks are skipped for very new sessions
    (< segment_time) to avoid false positives during the initial buffering phase.
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
        await asyncio.to_thread(_restart_channel_sync, channel_id, "process_dead")
        return

    # Process is alive — update heartbeat
    pm.mark_alive(channel_id)

    # Skip file checks during the startup grace period
    segment_seconds = _parse_segment_seconds(config.segmentation.segment_time)
    if uptime_seconds < segment_seconds:
        return

    tolerance = float(settings.watchdog_segment_tolerance_seconds)
    max_age = segment_seconds + tolerance

    record_dir = Path(config.paths.record_dir)
    newest_path, newest_mtime, newest_size = _get_newest_mp4(record_dir)

    # ── Check 2: file output age ───────────────────────────────────────────
    if newest_path is None:
        # No files after the grace period — suspicious
        with SessionLocal() as db:
            _log_event(
                db, channel_id, "no_new_files",
                f"No mp4 files found in {record_dir} after {uptime_seconds:.0f}s",
            )
            _log_segment_anomaly(db, channel_id, None, segment_seconds, uptime_seconds)
        pm.mark_unhealthy(channel_id)
        logger.info("[watchdog][%s] No output files — scheduling auto-restart.", channel_id)
        await asyncio.to_thread(_restart_channel_sync, channel_id, "no_new_files")
        return

    file_age = time.time() - newest_mtime
    if file_age > max_age:
        last_seg_ts = datetime.fromtimestamp(newest_mtime, tz=timezone.utc)
        with SessionLocal() as db:
            _log_event(
                db, channel_id, "no_new_files",
                f"Newest segment is {file_age:.1f}s old (max allowed {max_age:.1f}s)",
            )
            _log_segment_anomaly(db, channel_id, last_seg_ts, segment_seconds, file_age)
        pm.mark_unhealthy(channel_id)
        logger.info(
            "[watchdog][%s] Stale output (age=%.1fs, max=%.1fs) — scheduling restart.",
            channel_id, file_age, max_age,
        )
        await asyncio.to_thread(_restart_channel_sync, channel_id, "no_new_files")
        return

    # ── Check 3: stall detection (file size growth) — Phase 1.6 ───────────
    # Update stall tracking; returns True if the file is actively growing.
    growing = pm.update_stall_tracking(
        channel_id, str(newest_path), newest_size
    )
    if not growing:
        stall_secs = pm.get_stall_seconds(channel_id) or 0.0
        if stall_secs >= settings.stall_detection_seconds:
            with SessionLocal() as db:
                _log_event(
                    db, channel_id, "stalled_output",
                    f"File {newest_path.name} size unchanged for {stall_secs:.1f}s "
                    f"(threshold {settings.stall_detection_seconds}s)",
                )
                _log_segment_anomaly(
                    db, channel_id,
                    datetime.fromtimestamp(newest_mtime, tz=timezone.utc),
                    segment_seconds,
                    stall_secs,
                )
            pm.mark_unhealthy(channel_id)
            logger.info(
                "[watchdog][%s] Output stalled (%.1fs) — scheduling restart.",
                channel_id, stall_secs,
            )
            await asyncio.to_thread(_restart_channel_sync, channel_id, "stalled_output")


# ─── Watchdog loop ────────────────────────────────────────────────────────────

async def run_watchdog() -> None:
    """
    Check all currently running channels (called from the independent watchdog task).

    Iterates over a snapshot of pm._procs to avoid mutation during iteration.
    """
    pm = get_process_manager()
    running = list(pm._procs.items())
    if not running:
        return

    SessionLocal = get_session_factory()

    for channel_id, info in running:
        try:
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


async def run_watchdog_loop() -> None:
    """
    Independent watchdog task — runs its own interval loop.

    Started directly as an asyncio.Task in main.py lifespan, completely
    decoupled from the shared BackgroundScheduler so it is never delayed by
    file_mover or retention work.
    """
    settings = get_settings()
    interval = settings.watchdog_interval_seconds
    logger.info("Watchdog: independent loop started (interval=%ds).", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await run_watchdog()
        except Exception:
            logger.exception("Watchdog: unexpected error in main loop.")

