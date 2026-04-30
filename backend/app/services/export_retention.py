"""
Export Retention Service — Phase 2C.

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
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..config.settings import get_settings

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400.0


def _delete_old_files(root: Path, pattern: str, max_age_seconds: float) -> int:
    """
    Delete files matching *pattern* under *root* that are older than
    *max_age_seconds*.

    Returns the number of files deleted.
    """
    if not root.exists():
        return 0

    deleted = 0
    now = time.time()

    for f in list(root.rglob(pattern)):
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

    # Delete old exported video files
    mp4_deleted = _delete_old_files(settings.exports_dir, "*.mp4", max_age)

    # Delete old per-job log files
    log_deleted = _delete_old_files(settings.export_logs_dir, "export_*.log", max_age)

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
