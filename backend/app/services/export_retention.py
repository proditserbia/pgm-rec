"""
Export Retention Service — Phase 2C / 7.

Scheduled cleanup that removes old export files and their associated log files
once they exceed ``export_retention_days`` days of age.

The scheduler runs this in the same pool as the recording retention cleaner
(hourly by default).

Rules:
- Only deletes files/dirs under ``exports_dir`` and ``export_logs_dir``.
- Matches ``*.mp4`` and ``export_*.log`` files recursively.
- Deletes empty date subdirectories after cleaning files.
- ``export_retention_days = 0`` disables cleanup entirely.
- Individual file errors are caught and logged without aborting the run.
- Phase 7: ExportJob rows with ``never_expires=True`` are excluded — their
  output and log files are never deleted by the retention cleaner.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..config.settings import get_settings
from ..db.session import get_session_factory

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400.0


def _get_protected_paths() -> set[str]:
    """
    Phase 7 — return the set of output_path and log_path values for all
    ExportJob rows marked never_expires=True.

    These files must be skipped by the retention cleaner.
    """
    from ..db.models import ExportJob
    SessionLocal = get_session_factory()
    protected: set[str] = set()
    try:
        with SessionLocal() as db:
            rows = (
                db.query(ExportJob.output_path, ExportJob.log_path)
                .filter(ExportJob.never_expires.is_(True))
                .all()
            )
            for output_path, log_path in rows:
                if output_path:
                    protected.add(output_path)
                if log_path:
                    protected.add(log_path)
    except Exception:
        logger.exception("[export-retention] Failed to query protected export jobs.")
    return protected


def _delete_old_files(
    root: Path,
    pattern: str,
    max_age_seconds: float,
    protected: set[str] | None = None,
) -> int:
    """
    Delete files matching *pattern* under *root* that are older than
    *max_age_seconds*.

    *protected* is an optional set of absolute path strings that must not be
    deleted (Phase 7: never_expires jobs).

    Returns the number of files deleted.
    """
    if not root.exists():
        return 0

    deleted = 0
    now = time.time()

    for f in list(root.rglob(pattern)):
        # Phase 7 — skip never_expires files
        if protected and str(f) in protected:
            logger.debug("[export-retention] Skipping protected file: %s", f)
            continue
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue

        if age > max_age_seconds:
            try:
                f.unlink()
                logger.info(
                    "[export-retention] Deleted %s (age=%.1f days).",
                    f, age / _SECONDS_PER_DAY,
                )
                deleted += 1
            except OSError as exc:
                logger.error("[export-retention] Could not delete %s: %s", f, exc)

    return deleted


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty subdirectories under *root* (leaf-first)."""
    if not root.exists():
        return
    for d in sorted(root.rglob("*"), reverse=True):
        if d.is_dir() and d != root:
            try:
                d.rmdir()  # only removes if empty
                logger.debug("[export-retention] Removed empty dir: %s", d)
            except OSError:
                pass  # not empty or permission error — skip


def _run_export_retention_sync() -> None:
    """
    Scan exports_dir and export_logs_dir and delete files older than
    export_retention_days.

    No-op when export_retention_days == 0.
    """
    settings = get_settings()

    if settings.export_retention_days <= 0:
        logger.debug("[export-retention] Disabled (export_retention_days=0).")
        return

    max_age = settings.export_retention_days * _SECONDS_PER_DAY

    # Phase 7 — collect paths from never_expires jobs (skip their files)
    protected = _get_protected_paths()

    # Delete old exported video files
    mp4_deleted = _delete_old_files(settings.exports_dir, "*.mp4", max_age, protected)

    # Delete old per-job log files
    log_deleted = _delete_old_files(settings.export_logs_dir, "export_*.log", max_age, protected)

    # Prune now-empty date subdirectories in both trees
    _prune_empty_dirs(settings.exports_dir)
    _prune_empty_dirs(settings.export_logs_dir)

    if mp4_deleted or log_deleted:
        logger.info(
            "[export-retention] Deleted %d export file(s) and %d log file(s).",
            mp4_deleted, log_deleted,
        )


async def run_export_retention() -> None:
    """Async entry point called by the scheduler."""
    await asyncio.to_thread(_run_export_retention_sync)
