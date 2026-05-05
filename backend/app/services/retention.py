"""
Retention / Cleanup Service — Phase 1.5 / Phase 6.2 / Phase 23.

Replicates the behavior of del_rts1.bat:

  FOR %%Z IN (.mp4) do forfiles -p D:\\AutoRec\\record\\rts1\\3_final -s -m *%%Z
    -d -30 -c "cmd /c del @PATH"

Behavior:
- Runs on a configurable interval (default hourly).
- Phase 23 (date-folder mode): deletes ``*.mp4`` files in date sub-folders
  under ``record_root`` that are older than ``retention.days``.  Empty date
  folders are pruned afterwards.  Files whose DB record has ``never_expires``
  set are kept regardless of age.
- Legacy (1_record/3_final mode): deletes ``*.mp4`` files in the channel's
  ``3_final`` directory that are older than ``retention.days``.
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
from datetime import datetime, timedelta
from pathlib import Path

from ..config.settings import get_settings, resolve_channel_path
from ..db.models import Channel, RestartHistoryRecord, SegmentAnomaly, SegmentRecord, WatchdogEvent
from ..db.session import get_session_factory
from ..models.schemas import ChannelConfig
from ..utils import utc_now

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400.0


def _delete_old_recordings(final_dir: Path, max_age_seconds: float) -> int:
    """
    Delete *.mp4 files in *final_dir* that are older than *max_age_seconds*.

    Returns the number of files deleted.  Used for the legacy 3_final pipeline.
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


def _get_never_expires_filenames(channel_id: str) -> set[str]:
    """Return a set of filenames whose DB record has ``never_expires=True``."""
    SessionLocal = get_session_factory()
    try:
        with SessionLocal() as db:
            rows = (
                db.query(SegmentRecord.filename)
                .filter(
                    SegmentRecord.channel_id == channel_id,
                    SegmentRecord.never_expires.is_(True),
                )
                .all()
            )
            return {row[0] for row in rows}
    except Exception:
        logger.exception("[retention] Could not load never_expires list for '%s'.", channel_id)
        return set()


def _delete_old_recordings_date_folders(
    channel_id: str, record_root: Path, max_age_seconds: float
) -> int:
    """
    Phase 23 — Delete segments in date sub-folders under *record_root* that
    are older than *max_age_seconds*.

    Respects ``never_expires``:  files whose ``SegmentRecord.never_expires``
    DB flag is ``True`` are skipped regardless of age.

    After deleting individual files, empty date folders are also removed.

    Returns the number of files deleted.
    """
    if not record_root.exists():
        return 0

    never_expires = _get_never_expires_filenames(channel_id)
    deleted = 0
    now = time.time()

    try:
        date_folders = [d for d in record_root.iterdir() if d.is_dir()]
    except OSError:
        return 0

    for folder in date_folders:
        try:
            mp4_files = list(folder.glob("*.mp4"))
        except OSError:
            continue

        for mp4 in mp4_files:
            if mp4.name in never_expires:
                logger.debug("[retention] Keeping %s (never_expires=True).", mp4.name)
                continue
            try:
                age = now - mp4.stat().st_mtime
            except OSError:
                continue
            if age > max_age_seconds:
                try:
                    mp4.unlink()
                    logger.info(
                        "[retention] Deleted %s (age=%.1f days).",
                        mp4, age / _SECONDS_PER_DAY,
                    )
                    deleted += 1
                except OSError as exc:
                    logger.error("[retention] Could not delete %s: %s", mp4, exc)

    # Prune empty date folders
    _prune_empty_date_folders(record_root)

    return deleted


def _prune_empty_date_folders(record_root: Path) -> None:
    """
    Remove empty date sub-folders under *record_root*.

    A folder is considered empty when it contains no ``*.mp4`` files.
    Other file types are left intact; the folder is only removed when it
    contains no items at all.
    """
    try:
        for folder in list(record_root.iterdir()):
            if not folder.is_dir():
                continue
            try:
                children = list(folder.iterdir())
                if not children:
                    folder.rmdir()
                    logger.info("[retention] Removed empty date folder: %s", folder)
            except OSError as exc:
                logger.debug("[retention] Cannot prune folder %s: %s", folder, exc)
    except OSError:
        pass


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

    cutoff = utc_now() - timedelta(days=retention_days)
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
            rh_cutoff = utc_now() - timedelta(
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
                    max_age = config.retention.days * _SECONDS_PER_DAY
                    paths = config.paths

                    if paths.effective_use_date_folders and paths.record_root:
                        # Phase 23 — date-folder mode
                        record_root = resolve_channel_path(paths.record_root)
                        total_deleted += _delete_old_recordings_date_folders(
                            ch.id, record_root, max_age
                        )
                    elif paths.final_dir:
                        # Legacy mode — 3_final directory
                        final_dir = resolve_channel_path(paths.final_dir)
                        total_deleted += _delete_old_recordings(final_dir, max_age)
                    else:
                        logger.debug(
                            "[retention][%s] No retention target configured — skipping.", ch.id
                        )
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
