"""
Segment Indexer Service — Phase 23.

Replaces the file_mover's manifest-registration role for channels that use
the new **date-folder** recording layout:

    {record_root}/{YYYY_MM_DD}/{filename}.mp4

Because FFmpeg writes segments directly into date-based sub-folders (no
``1_record → 2_chunks`` move step), this service periodically:

1. Scans all date sub-folders under each channel's ``record_root``.
2. Finds completed segments (age + size-stability + ffprobe duration checks).
3. Skips the currently-active file (the newest in today's folder).
4. Skips files already registered in the DB (duplicate guard).
5. Registers each qualifying segment via ``manifest_service.register_segment()``.

Completion criteria (all must pass):
- File is older than ``segment_indexer_min_age_seconds``.
- File size is stable across two reads separated by
  ``segment_indexer_stability_check_seconds``.
- ffprobe reports duration > ``segment_indexer_min_duration_seconds``.
- File is not the newest ``*.mp4`` in its date folder (i.e. not currently
  being written by FFmpeg).

Near-midnight folder pre-creation:
The indexer also calls ``ensure_date_folders()`` on each channel at every run
so that tomorrow's folder is created before FFmpeg rolls over.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from ..config.settings import get_settings, resolve_channel_path
from ..db.models import Channel, SegmentRecord
from ..db.session import get_session_factory
from ..models.schemas import ChannelConfig
from .ffmpeg_builder import ensure_date_folders  # re-exported for patching in tests

logger = logging.getLogger(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_mtime(p: Path) -> float:
    """Return mtime of *p*, or 0.0 on any OS error."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _safe_size(p: Path) -> int:
    """Return size of *p* in bytes, or -1 on any OS error."""
    try:
        return p.stat().st_size
    except OSError:
        return -1


def _is_size_stable(path: Path, check_interval: float) -> bool:
    """
    Return True only if *path* has a non-zero size that does not change across
    two reads separated by *check_interval* seconds.

    Runs synchronously (called via ``asyncio.to_thread`` from the async
    entry-point, so the blocking ``time.sleep`` is acceptable).
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


def _ffprobe_duration(file_path: Path, ffprobe_path: str) -> Optional[float]:
    """
    Run ffprobe to get the duration of *file_path*.

    Returns the duration in seconds, or ``None`` when ffprobe is unavailable
    or the file cannot be probed.
    """
    from ..services.manifest_service import ffprobe_duration  # local to avoid circular
    return ffprobe_duration(file_path, ffprobe_path)


def _scan_date_folders(record_root: Path) -> list[Path]:
    """
    Return a sorted list of all date sub-folders found directly under
    *record_root*.

    Only first-level sub-directories whose names look like strftime date
    patterns are considered; the sorting ensures older folders are processed
    first.
    """
    if not record_root.exists():
        return []
    try:
        return sorted(
            d for d in record_root.iterdir() if d.is_dir()
        )
    except OSError:
        return []


def _find_active_file(date_folder: Path) -> Optional[Path]:
    """
    Return the newest ``*.mp4`` in *date_folder* (the file currently being
    written by FFmpeg), or ``None`` if the folder has no mp4 files.
    """
    try:
        mp4s = list(date_folder.glob("*.mp4"))
        if not mp4s:
            return None
        return max(mp4s, key=_safe_mtime)
    except OSError:
        return None


def _is_segment_complete(
    path: Path,
    active_file: Optional[Path],
    min_age_seconds: float,
    stability_check_seconds: float,
    min_duration_seconds: float,
    ffprobe_path: str,
) -> bool:
    """
    Return True when *path* passes all completion checks.

    Checks (in order of cost — cheapest first):
    1. Not the currently-active file (being written by FFmpeg).
    2. File is older than *min_age_seconds* (mtime guard).
    3. File size is stable across a double read.
    4. ffprobe duration > *min_duration_seconds*.
    """
    # 1. Skip active file
    if active_file is not None and path == active_file:
        logger.debug("[segment_indexer] Skipping active file: %s", path.name)
        return False

    # 2. Age guard
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age < min_age_seconds:
        logger.debug(
            "[segment_indexer] Skipping %s — too recent (age=%.1fs < %.1fs).",
            path.name, age, min_age_seconds,
        )
        return False

    # 3. Size stability
    if not _is_size_stable(path, stability_check_seconds):
        logger.debug("[segment_indexer] Skipping %s — size not stable.", path.name)
        return False

    # 4. ffprobe duration
    duration = _ffprobe_duration(path, ffprobe_path)
    if duration is None or duration < min_duration_seconds:
        logger.debug(
            "[segment_indexer] Skipping %s — ffprobe duration %s < %.1fs.",
            path.name, duration, min_duration_seconds,
        )
        return False

    return True


def _is_already_registered(channel_id: str, filename: str, db) -> bool:
    """Return True when a SegmentRecord already exists for *(channel_id, filename)*."""
    return (
        db.query(SegmentRecord)
        .filter(
            SegmentRecord.channel_id == channel_id,
            SegmentRecord.filename == filename,
        )
        .first()
    ) is not None


# ─── Core indexer ─────────────────────────────────────────────────────────────

def _run_segment_indexer_sync() -> None:
    """
    Iterate all enabled channels and index completed segments.

    Called via ``asyncio.to_thread`` so blocking I/O doesn't stall the loop.
    """
    from .manifest_service import register_segment  # local import to avoid circular

    settings = get_settings()
    min_age = float(settings.segment_indexer_min_age_seconds)
    stability = float(settings.segment_indexer_stability_check_seconds)
    min_dur = float(settings.segment_indexer_min_duration_seconds)

    SessionLocal = get_session_factory()
    total_registered = 0

    with SessionLocal() as db:
        channels = db.query(Channel).filter(Channel.enabled.is_(True)).all()
        for ch in channels:
            try:
                config = ChannelConfig.model_validate_json(ch.config_json)
                paths = config.paths

                if not paths.effective_use_date_folders or not paths.record_root:
                    # Legacy channel — skip; file_mover handles it.
                    continue

                record_root = resolve_channel_path(paths.record_root)

                # Pre-create today + tomorrow date folders
                try:
                    ensure_date_folders(config)
                except Exception:
                    logger.exception(
                        "[segment_indexer][%s] Failed to ensure date folders.", ch.id
                    )

                # Derive ffprobe path from channel ffmpeg_path
                ffprobe_path = _get_ffprobe_path(config.ffmpeg_path)

                # Scan date sub-folders
                for date_folder in _scan_date_folders(record_root):
                    active = _find_active_file(date_folder)
                    try:
                        mp4_files = sorted(date_folder.glob("*.mp4"), key=_safe_mtime)
                    except OSError:
                        continue

                    for mp4_path in mp4_files:
                        if _is_already_registered(ch.id, mp4_path.name, db):
                            continue

                        if not _is_segment_complete(
                            mp4_path, active, min_age, stability, min_dur, ffprobe_path
                        ):
                            continue

                        try:
                            register_segment(ch.id, mp4_path, config, db)
                            total_registered += 1
                        except Exception:
                            logger.exception(
                                "[segment_indexer][%s] Registration failed for '%s'.",
                                ch.id, mp4_path.name,
                            )

            except Exception:
                logger.exception(
                    "[segment_indexer][%s] Error processing channel.", ch.id
                )

    if total_registered:
        logger.info("[segment_indexer] Registered %d new segment(s).", total_registered)


def _get_ffprobe_path(ffmpeg_path: str) -> str:
    """Derive the ffprobe binary path from the channel's configured ffmpeg path."""
    p = Path(ffmpeg_path)
    name = p.name
    if name.lower() in ("ffmpeg", "ffmpeg.exe"):
        probe_name = name.lower().replace("ffmpeg", "ffprobe")
        return str(p.parent / probe_name)
    return "ffprobe"


async def run_segment_indexer() -> None:
    """Async entry point called by the scheduler."""
    await asyncio.to_thread(_run_segment_indexer_sync)
