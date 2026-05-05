"""
Retention / Cleanup Service — Phase 1.5 / Phase 6.2 / Phase 23 / Phase 25.

Replicates the behavior of del_rts1.bat:

  FOR %%Z IN (.mp4) do forfiles -p D:\\AutoRec\\record\\rts1\\3_final -s -m *%%Z
    -d -30 -c "cmd /c del @PATH"

Phase 25 additions
──────────────────
- ``dry_run`` mode: scan what *would* be deleted without touching the filesystem.
- Date-folder-name-based deletion: eligibility is determined by parsing the
  folder name, not file mtime, so recordings in correctly-named date folders
  are always handled consistently.
- Current-day protection: today's date folder is **never** touched.
- Global enable/disable via ``PGMREC_RECORDING_RETENTION_ENABLED``.
- DB pruning: when ``PGMREC_PRUNE_SEGMENT_DB_AFTER_DELETE=true``, deleted
  SegmentRecord rows are marked with ``file_exists=False`` / ``deleted_at=<now>``.
- ``run_channel_retention()`` public async function for the API endpoint.
- ``RetentionResult`` dataclass for dry-run and live reporting.

Behavior:
- Runs on a configurable interval (default hourly).
- Phase 23 (date-folder mode): deletes ``*.mp4`` files in date sub-folders
  under ``record_root`` that are in folders older than ``retention.days``.
  Eligibility is decided by parsing the folder name, not file mtime.
  Empty date folders are pruned afterwards.
  Files whose DB record has ``never_expires`` set are kept regardless of age.
- Legacy (1_record/3_final mode): deletes ``*.mp4`` files in the channel's
  ``3_final`` directory that are older than ``retention.days`` (mtime-based).
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
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..config.settings import get_settings, resolve_channel_path
from ..db.models import Channel, RestartHistoryRecord, SegmentAnomaly, SegmentRecord, WatchdogEvent
from ..db.session import get_session_factory
from ..models.schemas import (
    ChannelConfig,
    RetentionChannelResult,
    RetentionRunResponse,
)
from ..utils import utc_now

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400.0


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class _RetentionResult:
    """Internal tracking of what was (or would be) deleted for one channel."""
    channel_id: str
    files_deleted: int = 0
    folders_deleted: int = 0
    total_bytes: int = 0
    files_to_delete: list[str] = field(default_factory=list)
    folders_to_delete: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None

    def to_schema(self) -> RetentionChannelResult:
        return RetentionChannelResult(
            channel_id=self.channel_id,
            skipped=self.skipped,
            skip_reason=self.skip_reason,
            files_deleted=self.files_deleted,
            folders_deleted=self.folders_deleted,
            total_bytes=self.total_bytes,
            files_to_delete=self.files_to_delete,
            folders_to_delete=self.folders_to_delete,
        )


# ─── Timezone helper ──────────────────────────────────────────────────────────

def _get_local_today(tz_name: str) -> _date:
    """Return today's date in *tz_name* (falls back to UTC on error)."""
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            tz = ZoneInfo(tz_name)
            return datetime.now(tz).date()
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning(
                "[retention] Timezone '%s' not found — falling back to UTC.", tz_name
            )
    except ImportError:
        pass
    return datetime.now(timezone.utc).date()


# ─── never_expires helpers ────────────────────────────────────────────────────

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


# ─── DB pruning ───────────────────────────────────────────────────────────────

def _mark_segments_deleted_in_db(channel_id: str, filenames: list[str]) -> None:
    """
    Phase 25 — mark deleted segment files in the DB.

    Sets ``file_exists=False`` and ``deleted_at=utc_now()`` for every
    SegmentRecord matching *(channel_id, filename)*.

    Never deletes rows; the manifest and audit trail are preserved.
    """
    if not filenames:
        return
    SessionLocal = get_session_factory()
    now = utc_now()
    try:
        with SessionLocal() as db:
            db.query(SegmentRecord).filter(
                SegmentRecord.channel_id == channel_id,
                SegmentRecord.filename.in_(filenames),
                SegmentRecord.file_exists.is_(True),
            ).update(
                {"file_exists": False, "deleted_at": now},
                synchronize_session=False,
            )
            db.commit()
    except Exception:
        logger.exception(
            "[retention] Failed to mark segments as deleted for '%s'.", channel_id
        )


# ─── Date-folder helpers ──────────────────────────────────────────────────────

def _parse_folder_date(folder: Path, date_folder_format: str) -> Optional[_date]:
    """
    Parse a date-folder name using *date_folder_format*.

    Returns the parsed ``datetime.date`` or ``None`` if parsing fails.
    """
    try:
        return datetime.strptime(folder.name, date_folder_format).date()
    except (ValueError, TypeError):
        return None


def _scan_date_folder(
    channel_id: str,
    folder: Path,
    never_expires: set[str],
    dry_run: bool,
    result: _RetentionResult,
    prune_db: bool,
) -> None:
    """
    Scan one date folder for deletable ``.mp4`` files.

    In non-dry_run mode, actually deletes the files and optionally marks them
    in the DB.  Folder itself is not removed here — that is handled by
    ``_prune_empty_date_folders``.
    """
    try:
        mp4_files = list(folder.glob("*.mp4"))
    except OSError:
        return

    deleted_filenames: list[str] = []

    for mp4 in mp4_files:
        if mp4.name in never_expires:
            logger.debug("[retention][%s] Keeping %s (never_expires=True).", channel_id, mp4.name)
            continue

        try:
            size = mp4.stat().st_size
        except OSError:
            size = 0

        result.files_to_delete.append(str(mp4))

        if not dry_run:
            try:
                mp4.unlink()
                logger.info(
                    "[retention][%s] Deleted %s (folder=%s).",
                    channel_id, mp4.name, folder.name,
                )
                result.files_deleted += 1
                result.total_bytes += size
                deleted_filenames.append(mp4.name)
            except OSError as exc:
                logger.error("[retention][%s] Could not delete %s: %s", channel_id, mp4, exc)
        else:
            result.files_deleted += 1
            result.total_bytes += size

    if not dry_run and prune_db and deleted_filenames:
        _mark_segments_deleted_in_db(channel_id, deleted_filenames)


def _scan_date_folders_for_retention(
    channel_id: str,
    record_root: Path,
    retention_days: int,
    date_folder_format: str,
    channel_tz: str,
    dry_run: bool = False,
    prune_db: bool = False,
) -> _RetentionResult:
    """
    Phase 25 — scan date sub-folders under *record_root* and delete eligible
    ``*.mp4`` files.

    Eligibility rules (date-folder-name-based, not mtime):
    - The folder name is parsed using *date_folder_format*.
    - Folders where the parsed date is **today or in the future** are always
      skipped (current-day protection).
    - Folders where the parsed date is within the retention window
      (``folder_date >= cutoff``) are skipped.
    - Folders that cannot be parsed as a date are silently skipped.
    - Files with ``never_expires=True`` in the DB are kept regardless.

    After eligible files are removed, empty date folders are pruned.
    In ``dry_run=True`` mode, nothing is modified on disk or in the DB.
    """
    result = _RetentionResult(channel_id=channel_id)

    if not record_root.exists():
        logger.debug("[retention][%s] record_root '%s' does not exist — skipping.", channel_id, record_root)
        return result

    today = _get_local_today(channel_tz)
    cutoff = today - timedelta(days=retention_days)
    never_expires = _get_never_expires_filenames(channel_id)

    try:
        date_folders = [d for d in record_root.iterdir() if d.is_dir()]
    except OSError:
        return result

    for folder in date_folders:
        folder_date = _parse_folder_date(folder, date_folder_format)

        if folder_date is None:
            # Unrecognised folder name — skip safely
            logger.debug(
                "[retention][%s] Skipping unrecognised folder '%s' (not a date folder).",
                channel_id, folder.name,
            )
            continue

        if folder_date >= today:
            # Never touch today's or future folders
            logger.debug(
                "[retention][%s] Skipping current/future date folder '%s'.",
                channel_id, folder.name,
            )
            continue

        if folder_date >= cutoff:
            # Within the retention window — keep
            continue

        # This folder is past the retention cutoff — scan for deletable files
        _scan_date_folder(channel_id, folder, never_expires, dry_run, result, prune_db)

    # After deletions, prune empty date folders (not in dry-run mode)
    if not dry_run:
        _prune_empty_date_folders_result(record_root, result)
    else:
        # Dry-run: report which folders *would* become empty
        _collect_empty_date_folders(record_root, today, cutoff, date_folder_format, result)

    return result


# ─── Legacy mode helpers ──────────────────────────────────────────────────────

def _delete_old_recordings_legacy(
    channel_id: str,
    final_dir: Path,
    max_age_seconds: float,
    dry_run: bool = False,
    prune_db: bool = False,
) -> _RetentionResult:
    """
    Delete ``*.mp4`` files in *final_dir* that are older than *max_age_seconds*.

    Used for the legacy 3_final pipeline.  Returns a _RetentionResult.
    """
    result = _RetentionResult(channel_id=channel_id)
    if not final_dir.exists():
        return result

    never_expires = _get_never_expires_filenames(channel_id)
    now = time.time()
    deleted_filenames: list[str] = []

    for mp4 in list(final_dir.rglob("*.mp4")):
        if mp4.name in never_expires:
            logger.debug("[retention][%s] Keeping %s (never_expires=True).", channel_id, mp4.name)
            continue
        try:
            age = now - mp4.stat().st_mtime
            size = mp4.stat().st_size
        except OSError:
            continue

        if age > max_age_seconds:
            result.files_to_delete.append(str(mp4))
            if not dry_run:
                try:
                    mp4.unlink()
                    logger.info(
                        "[retention][%s] Deleted %s (age=%.1f days).",
                        channel_id, mp4, age / _SECONDS_PER_DAY,
                    )
                    result.files_deleted += 1
                    result.total_bytes += size
                    deleted_filenames.append(mp4.name)
                except OSError as exc:
                    logger.error("[retention][%s] Could not delete %s: %s", channel_id, mp4, exc)
            else:
                result.files_deleted += 1
                result.total_bytes += size

    if not dry_run and prune_db and deleted_filenames:
        _mark_segments_deleted_in_db(channel_id, deleted_filenames)

    return result


# ─── Folder pruning helpers ───────────────────────────────────────────────────

def _prune_empty_date_folders_result(record_root: Path, result: _RetentionResult) -> None:
    """
    Remove empty date sub-folders under *record_root* after file deletion.

    A folder is removed when it contains no items at all (regardless of type).
    Reports deleted folders in *result*.
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
                    result.folders_to_delete.append(str(folder))
                    result.folders_deleted += 1
            except OSError as exc:
                logger.debug("[retention] Cannot prune folder %s: %s", folder, exc)
    except OSError:
        pass


def _collect_empty_date_folders(
    record_root: Path,
    today: _date,
    cutoff: _date,
    date_folder_format: str,
    result: _RetentionResult,
) -> None:
    """
    Dry-run helper: collect date folders that *would* be empty after deletion.

    A folder is considered a candidate if it falls before *cutoff* and every
    ``.mp4`` file in it is already scheduled for deletion.
    """
    try:
        for folder in record_root.iterdir():
            if not folder.is_dir():
                continue
            folder_date = _parse_folder_date(folder, date_folder_format)
            if folder_date is None or folder_date >= today or folder_date >= cutoff:
                continue
            # Check whether all mp4s would be deleted (i.e. they're all already
            # in files_to_delete) and folder would become empty.
            try:
                children = list(folder.iterdir())
                if not children:
                    result.folders_to_delete.append(str(folder))
                    result.folders_deleted += 1
            except OSError:
                pass
    except OSError:
        pass


# ─── Legacy _prune_empty_date_folders (kept for Phase 23 compatibility) ───────

def _prune_empty_date_folders(record_root: Path) -> None:
    """
    Remove empty date sub-folders under *record_root*.

    A folder is considered empty when it contains no items at all.
    Other file types are left intact; the folder is only removed when it
    contains no items at all.

    Kept as a public helper for Phase 23 test compatibility.
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


# ─── _delete_old_recordings (legacy, Phase 23 compat) ────────────────────────

def _delete_old_recordings(final_dir: Path, max_age_seconds: float) -> int:
    """
    Delete *.mp4 files in *final_dir* that are older than *max_age_seconds*.

    Returns the number of files deleted.  Used for the legacy 3_final pipeline.
    Kept for backward compatibility with Phase 23 tests.
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


# ─── _delete_old_recordings_date_folders (Phase 23 compat shim) ──────────────

def _delete_old_recordings_date_folders(
    channel_id: str, record_root: Path, max_age_seconds: float
) -> int:
    """
    Phase 23 — Delete segments in date sub-folders under *record_root* that
    are older than *max_age_seconds* (file mtime-based).

    Respects ``never_expires``:  files whose ``SegmentRecord.never_expires``
    DB flag is ``True`` are skipped regardless of age.

    After deleting individual files, empty date folders are also removed.

    Returns the number of files deleted.

    Note: This legacy signature uses file mtime for backward compatibility
    with Phase 23 tests.  New code should use ``_scan_date_folders_for_retention``
    (date-folder-name-based, Phase 25).
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


# ─── Log file cleanup ─────────────────────────────────────────────────────────

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


# ─── Event table pruning (Phase 6.2) ─────────────────────────────────────────

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


# ─── Per-channel retention runner ────────────────────────────────────────────

def _run_channel_retention_sync(
    channel_id: str,
    config: ChannelConfig,
    dry_run: bool = False,
) -> _RetentionResult:
    """
    Run (or simulate) retention cleanup for a single channel.

    Returns a ``_RetentionResult`` describing what was (or would be) deleted.

    This is the core function used by both the scheduler and the API endpoint.
    """
    settings = get_settings()

    # Global enable/disable
    if not settings.recording_retention_enabled:
        return _RetentionResult(
            channel_id=channel_id,
            skipped=True,
            skip_reason="recording_retention_enabled=False",
        )

    # Per-channel enable/disable
    if not config.retention.enabled:
        return _RetentionResult(
            channel_id=channel_id,
            skipped=True,
            skip_reason="channel retention.enabled=False",
        )

    retention_days = config.retention.days
    prune_db = settings.prune_segment_db_after_delete

    # Channel timezone (fall back to manifest_timezone, then UTC)
    channel_tz = getattr(settings, "manifest_timezone", "Europe/Belgrade")

    paths = config.paths

    if paths.effective_use_date_folders and paths.record_root:
        # Phase 23/25 — date-folder mode
        record_root = resolve_channel_path(paths.record_root)
        return _scan_date_folders_for_retention(
            channel_id=channel_id,
            record_root=record_root,
            retention_days=retention_days,
            date_folder_format=paths.date_folder_format,
            channel_tz=channel_tz,
            dry_run=dry_run,
            prune_db=prune_db,
        )

    elif paths.final_dir:
        # Legacy mode — 3_final directory (mtime-based)
        final_dir = resolve_channel_path(paths.final_dir)
        max_age = retention_days * _SECONDS_PER_DAY
        return _delete_old_recordings_legacy(
            channel_id=channel_id,
            final_dir=final_dir,
            max_age_seconds=max_age,
            dry_run=dry_run,
            prune_db=prune_db,
        )

    else:
        return _RetentionResult(
            channel_id=channel_id,
            skipped=True,
            skip_reason="No retention target configured (no record_root or final_dir).",
        )


# ─── Scheduler entry point (runs all channels) ───────────────────────────────

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
                result = _run_channel_retention_sync(ch.id, config, dry_run=False)
                if not result.skipped:
                    total_deleted += result.files_deleted

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


# ─── Public async API for the retention endpoint ─────────────────────────────

async def run_channel_retention(
    channel_id: Optional[str],
    dry_run: bool,
) -> RetentionRunResponse:
    """
    Async entry point for the ``POST /api/v1/retention/run`` endpoint.

    Runs retention for the specified channel (or all channels if
    *channel_id* is ``None``).  When *dry_run=True*, no files are
    modified — the response shows what *would* be deleted.

    Returns a :class:`RetentionRunResponse` with per-channel details.
    """
    def _scan() -> RetentionRunResponse:
        SessionLocal = get_session_factory()
        channel_results: list[_RetentionResult] = []

        with SessionLocal() as db:
            if channel_id:
                ch = db.query(Channel).filter(Channel.id == channel_id).first()
                channels_to_scan = [ch] if ch else []
            else:
                channels_to_scan = db.query(Channel).all()

        for ch in channels_to_scan:
            try:
                config = ChannelConfig.model_validate_json(ch.config_json)
                result = _run_channel_retention_sync(ch.id, config, dry_run=dry_run)
                channel_results.append(result)
            except Exception as exc:
                logger.error("[retention][%s] Error during API run: %s", ch.id, exc)
                channel_results.append(
                    _RetentionResult(
                        channel_id=ch.id,
                        skipped=True,
                        skip_reason=f"Error: {exc}",
                    )
                )

        total_files = sum(r.files_deleted for r in channel_results)
        total_folders = sum(r.folders_deleted for r in channel_results)
        total_bytes = sum(r.total_bytes for r in channel_results)

        return RetentionRunResponse(
            dry_run=dry_run,
            executed=not dry_run,
            channels=[r.to_schema() for r in channel_results],
            total_files_deleted=total_files,
            total_folders_deleted=total_folders,
            total_bytes=total_bytes,
        )

    return await asyncio.to_thread(_scan)

