"""
File Mover Service — Phase 1.5 / 1.6 / 2A.

Replicates the behavior of move_rts1.bat:

  pushd D:\\AutoRec\\record\\rts1\\1_record
  move *.mp4 D:\\AutoRec\\record\\rts1\\2_chunks

Rules:
- Runs on a configurable interval (default 30 s).
- Only moves files that are "complete" — not currently being written by FFmpeg.
- Phase 1.5: a file must be at least file_mover_min_age_seconds old.
- Phase 1.6: additionally, the file size must be stable across two reads
  separated by file_mover_stability_check_seconds (double-check guard).
  This catches disk-lag scenarios where the mtime has stopped updating but
  the file is still being written (e.g. buffered I/O on slow storage).
- Phase 2A: after moving a file, register it in the recording manifest and DB.
- Destination directory is created if it doesn't exist.
- Each moved file is logged.
- Errors on individual files are logged but do not abort the whole run.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from ..config.settings import resolve_channel_path
from ..db.models import Channel
from ..db.session import get_session_factory
from ..models.schemas import ChannelConfig

logger = logging.getLogger(__name__)


def _is_size_stable(path: Path, check_interval: float) -> bool:
    """
    Read *path*'s size twice, separated by *check_interval* seconds.

    Returns True only if:
    - Both reads succeed.
    - The file is non-empty.
    - The size is identical in both readings.

    Running in a thread pool (via asyncio.to_thread) so the sleep here is fine.
    """
    try:
        size1 = path.stat().st_size
        if size1 == 0:
            return False
        time.sleep(check_interval)
        size2 = path.stat().st_size
        return size1 == size2
    except OSError:
        return False


def _move_completed_files(
    record_dir: Path,
    chunks_dir: Path,
    min_age_seconds: float,
    stability_check_seconds: float,
) -> list[Path]:
    """
    Move all *.mp4 files from *record_dir* that pass both the age check and
    the double-read size-stability check into *chunks_dir*.

    Returns the list of destination paths for successfully moved files
    (used by the caller to trigger manifest registration).
    """
    if not record_dir.exists():
        return []

    chunks_dir.mkdir(parents=True, exist_ok=True)

    moved: list[Path] = []
    now = time.time()

    for src in list(record_dir.glob("*.mp4")):
        try:
            stat = src.stat()
        except OSError:
            continue  # file disappeared between glob and stat — skip

        # Age guard (Phase 1.5)
        age = now - stat.st_mtime
        if age < min_age_seconds:
            logger.debug(
                "[file_mover] Skipping %s — too recent (age=%.1fs < %.1fs).",
                src.name, age, min_age_seconds,
            )
            continue

        # Size-stability double-check (Phase 1.6)
        if not _is_size_stable(src, stability_check_seconds):
            logger.debug(
                "[file_mover] Skipping %s — size not stable yet.", src.name
            )
            continue

        dest = chunks_dir / src.name
        if dest.exists():
            # Phase 9 — idempotent handling of pre-existing destinations.
            try:
                src_size = src.stat().st_size
                dest_size = dest.stat().st_size
            except OSError:
                continue

            if src_size == dest_size:
                # Source and destination are the same file (e.g. previous move
                # succeeded but crashed before cleaning up the source).
                # Remove the stale source and treat this as already moved.
                try:
                    src.unlink()
                    logger.info(
                        "[file_mover] Removed stale duplicate source %s "
                        "(dest already exists with same size=%d).",
                        src.name, dest_size,
                    )
                except OSError as exc:
                    logger.warning(
                        "[file_mover] Could not remove stale source %s: %s",
                        src.name, exc,
                    )
                continue
            else:
                # Genuine conflict: dest exists but sizes differ.
                # Rename the destination to a safe backup name, then proceed.
                # Path.with_stem() requires Python 3.9+ (same requirement as
                # the rest of the project which targets 3.9+).
                suffix = int(time.time())
                backup = dest.with_stem(f"{dest.stem}_conflict_{suffix}")
                try:
                    dest.rename(backup)
                    logger.warning(
                        "[file_mover] Destination conflict for %s "
                        "(src=%d bytes, dest=%d bytes) — "
                        "renamed dest to %s, proceeding with move.",
                        src.name, src_size, dest_size, backup.name,
                    )
                except OSError as exc:
                    logger.error(
                        "[file_mover] Cannot resolve destination conflict for %s: %s",
                        src.name, exc,
                    )
                    continue

        try:
            shutil.move(str(src), str(dest))
            logger.info("[file_mover] Moved %s → %s", src, dest)
            moved.append(dest)
        except OSError as exc:
            logger.error("[file_mover] Failed to move %s: %s", src, exc)

    return moved


def _run_file_mover_sync() -> None:
    """
    Iterate all channels.

    Called via asyncio.to_thread so file I/O doesn't block the event loop.

    All channels using ``paths.record_root`` (date-folder mode) are handled by
    the ``segment_indexer`` service.  Channels that still have legacy
    ``record_dir``/``chunks_dir`` configured are skipped with a WARNING —
    those paths are no longer supported.  Migrate to ``paths.record_root``.
    """
    SessionLocal = get_session_factory()

    with SessionLocal() as db:
        channels = db.query(Channel).filter(Channel.enabled.is_(True)).all()
        for ch in channels:
            try:
                config = ChannelConfig.model_validate_json(ch.config_json)

                # Skip date-folder channels (handled by segment_indexer)
                if config.paths.effective_use_date_folders:
                    logger.debug(
                        "[file_mover][%s] Skipping — channel uses date-folder mode "
                        "(record_root). Handled by segment_indexer.",
                        ch.id,
                    )
                    continue

                # Legacy channel — record_dir/chunks_dir are no longer supported.
                # Log a warning and skip. Migrate to paths.record_root.
                if not config.paths.record_dir or not config.paths.chunks_dir:
                    logger.debug(
                        "[file_mover][%s] Skipping — no record_dir/chunks_dir configured.",
                        ch.id,
                    )
                    continue

                logger.warning(
                    "[file_mover][%s] Legacy record_dir/chunks_dir detected but ignored. "
                    "Migrate to paths.record_root (date-folder mode). Skipping.",
                    ch.id,
                )
            except Exception:
                logger.exception("[file_mover][%s] Error processing channel.", ch.id)


async def run_file_mover() -> None:
    """Async entry point called by the scheduler."""
    await asyncio.to_thread(_run_file_mover_sync)
