"""
Retention / Cleanup Service — Phase 1.5 / Phase 6.2.

Replicates the behavior of del_rts1.bat:

  FOR %%Z IN (.mp4) do forfiles -p D:\\AutoRec\\record\\rts1\\3_final -s -m *%%Z
    -d -30 -c "cmd /c del @PATH"

Behavior:
- Runs on a configurable interval (default hourly).
- Deletes *.mp4 files in the channel's 3_final directory that are older than
  retention.days (default 30 days).
- Per-channel opt-in: skips channels with retention.enabled = False.
- Every deletion is logged (file path, age).
- Also cleans up old FFmpeg log files beyond the per-channel limit
  (log_max_files_per_channel).
- Errors on individual files are caught and logged without aborting the run.

Phase 6.2 additions:
- Prunes watchdog_events and segment_anomalies older than
  event_retention_days (default 90 days) to prevent unbounded DB growth.
- Prunes restart_history older than the backoff window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config.settings import get_settings
from ..db.models import Channel, RestartHistoryRecord, SegmentAnomaly, WatchdogEvent
from ..db.session import get_session_factory
from ..models.schemas import ChannelConfig

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400.0


def _delete_old_recordings(final_dir: Path, max_age_seconds: float) -> int:
    """
    Delete *.mp4 files in *final_dir* that are older than *max_age_seconds*.

    Returns the number of files deleted.
    """
    if not final_dir.exists():
        return 0

    deleted = 0
    now = time.time()

    for mp4 in list(final_dir.rglob("*.mp4")):
        try:
            age = now - mp4.stat().st_mtime
        except OSError:
            continue

        if age > max_age_seconds:
            try:
                mp4.unlink()
                logger.info(
                    "[retention] Deleted %s (age=%.1f days).", mp4, age / _SECONDS_PER_DAY
                )
                deleted += 1
            except OSError as exc:
                logger.error("[retention] Could not delete %s: %s", mp4, exc)

    return deleted


def _prune_log_files(channel_id: str, log_max: int) -> None:
    """Delete oldest FFmpeg log files beyond *log_max* for *channel_id*."""
    settings = get_settings()
    log_dir = settings.logs_dir / "channels" / channel_id
    if not log_dir.exists():
        return

    files = sorted(log_dir.glob("ffmpeg-*.log"))
    excess = files[: max(0, len(files) - log_max)]
    for f in excess:
        try:
            f.unlink()
            logger.info("[retention] Pruned log file: %s", f.name)
        except OSError as exc:
            logger.warning("[retention] Could not delete log %s: %s", f, exc)


def _prune_event_tables() -> None:
    """
    Phase 6.2 — delete old rows from watchdog_events, segment_anomalies,
    and restart_history to prevent unbounded DB growth.
    """
    settings = get_settings()
    retention_days = settings.event_retention_days
    if retention_days <= 0:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    SessionLocal = get_session_factory()

    try:
        with SessionLocal() as db:
            we_deleted = (
                db.query(WatchdogEvent)
                .filter(WatchdogEvent.detected_at < cutoff)
                .delete(synchronize_session=False)
            )
            sa_deleted = (
                db.query(SegmentAnomaly)
                .filter(SegmentAnomaly.detected_at < cutoff)
                .delete(synchronize_session=False)
            )
            # Restart history only needs to cover the backoff window + a margin
            rh_cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=settings.restart_backoff_window_seconds * 2
            )
            rh_deleted = (
                db.query(RestartHistoryRecord)
                .filter(RestartHistoryRecord.attempted_at < rh_cutoff)
                .delete(synchronize_session=False)
            )
            db.commit()

        if we_deleted or sa_deleted or rh_deleted:
            logger.info(
                "[retention] Pruned DB rows — watchdog_events: %d, "
                "segment_anomalies: %d, restart_history: %d.",
                we_deleted, sa_deleted, rh_deleted,
            )
    except Exception:
        logger.exception("[retention] Error pruning event tables.")


def _run_retention_sync() -> None:
    """
    Iterate all channels and apply retention policy.

    Runs in a thread pool so file I/O doesn't block the event loop.
    """
    settings = get_settings()
    log_max = settings.log_max_files_per_channel
    SessionLocal = get_session_factory()
    total_deleted = 0

    with SessionLocal() as db:
        channels = db.query(Channel).all()
        for ch in channels:
            try:
                config = ChannelConfig.model_validate_json(ch.config_json)

                # Recording file retention
                if config.retention.enabled:
                    final_dir = Path(config.paths.final_dir)
                    max_age = config.retention.days * _SECONDS_PER_DAY
                    total_deleted += _delete_old_recordings(final_dir, max_age)
                else:
                    logger.debug(
                        "[retention][%s] Retention disabled — skipping.", ch.id
                    )

                # Log file cleanup (always runs)
                _prune_log_files(ch.id, log_max)

            except Exception:
                logger.exception("[retention][%s] Error processing channel.", ch.id)

    if total_deleted:
        logger.info("[retention] Deleted %d old recording file(s) total.", total_deleted)

    # Phase 6.2 — prune unbounded event/history tables
    _prune_event_tables()


async def run_retention() -> None:
    """Async entry point called by the scheduler."""
    await asyncio.to_thread(_run_retention_sync)
