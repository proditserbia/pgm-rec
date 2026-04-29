"""
File Mover Service — Phase 1.5.

Replicates the behavior of move_rts1.bat:

  pushd D:\\AutoRec\\record\\rts1\\1_record
  move *.mp4 D:\\AutoRec\\record\\rts1\\2_chunks

Rules:
- Runs on a configurable interval (default 30 s).
- Only moves files that are "complete" — not currently being written by FFmpeg.
- A file is considered complete when it has not been modified for at least
  file_mover_min_age_seconds (default 30 s).
  This is the portable, cross-platform equivalent of checking whether a file
  is locked (no need for OS-specific locking APIs).
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

from ..config.settings import get_settings
from ..db.models import Channel
from ..db.session import get_session_factory
from ..models.schemas import ChannelConfig

logger = logging.getLogger(__name__)


def _move_completed_files(
    record_dir: Path,
    chunks_dir: Path,
    min_age_seconds: float,
) -> int:
    """
    Move all *.mp4 files from *record_dir* that are older than *min_age_seconds*
    into *chunks_dir*.

    Returns the number of files successfully moved.
    """
    if not record_dir.exists():
        return 0

    chunks_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    now = time.time()

    for src in list(record_dir.glob("*.mp4")):
        try:
            stat = src.stat()
        except OSError:
            continue  # file disappeared between glob and stat — skip

        age = now - stat.st_mtime
        if age < min_age_seconds:
            logger.debug(
                "[file_mover] Skipping %s — too recent (age=%.1fs < %.1fs).",
                src.name, age, min_age_seconds,
            )
            continue

        dest = chunks_dir / src.name
        # If a file with the same name already exists in chunks_dir, skip to
        # avoid overwriting. (Shouldn't happen with strftime naming but be safe.)
        if dest.exists():
            logger.warning(
                "[file_mover] Destination already exists, skipping: %s", dest
            )
            continue

        try:
            shutil.move(str(src), str(dest))
            logger.info("[file_mover] Moved %s → %s", src, dest)
            moved += 1
        except OSError as exc:
            logger.error("[file_mover] Failed to move %s: %s", src, exc)

    return moved


def _run_file_mover_sync() -> None:
    """
    Iterate all channels and move completed files.

    Called via asyncio.to_thread so file I/O doesn't block the event loop.
    """
    settings = get_settings()
    min_age = float(settings.file_mover_min_age_seconds)
    SessionLocal = get_session_factory()
    total_moved = 0

    with SessionLocal() as db:
        channels = db.query(Channel).filter(Channel.enabled.is_(True)).all()
        for ch in channels:
            try:
                config = ChannelConfig.model_validate_json(ch.config_json)
                record_dir = Path(config.paths.record_dir)
                chunks_dir = Path(config.paths.chunks_dir)
                moved = _move_completed_files(record_dir, chunks_dir, min_age)
                total_moved += moved
            except Exception:
                logger.exception("[file_mover][%s] Error processing channel.", ch.id)

    if total_moved:
        logger.info("[file_mover] Moved %d file(s) total.", total_moved)


async def run_file_mover() -> None:
    """Async entry point called by the scheduler."""
    await asyncio.to_thread(_run_file_mover_sync)
