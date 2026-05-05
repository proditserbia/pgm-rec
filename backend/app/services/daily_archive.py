"""
Daily Archive Service — Phase 24.

Creates a 24-hour archive file for each enabled channel once per day using
the manifest DB index (SegmentRecord.manifest_date) to concatenate completed
segments without re-scanning the file system.

Architecture
────────────
A scheduler job calls :func:`run_daily_archive` every minute.  When the
current local time (in ``daily_archive_timezone``) reaches or passes
``daily_archive_time`` (HH:MM), the service archives the **previous calendar
day** for each configured channel.

Deduplication:
  An ExportJob row with ``job_source="daily_archive"`` for the same
  ``(channel_id, date)`` is created before FFmpeg starts.  If a non-failed
  job already exists, the channel is skipped for that date.  Failed jobs are
  **not** retried automatically (delete the row to trigger a re-run).

Output naming:
  Filename: ``{channel.name} {YYYYMMDD} 00-24.mp4``
  Folder priority:
    1. ``settings.daily_archive_dir``          (global override)
    2. ``paths.final_dir``                     (channel config)
    3. ``{paths.record_root}/archive``         (date-folder channels)
    4. ``{exports_dir}/{channel_id}/archive``  (fallback)

Public API
────────────
- ``run_daily_archive()``  — async entry point for the scheduler
- ``_get_archive_output_path(channel_id, config, target_date_str)`` → Path
- ``_is_already_archived(channel_id, target_date_str, db)``         → bool
- ``_get_segments_for_date(channel_id, target_date_str, db)``       → list
- ``_build_daily_archive_concat(segments, concat_path)``            → None
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..config.settings import get_settings, resolve_channel_path
from ..db.models import ExportJob, SegmentRecord
from ..db.session import get_session_factory
from ..models.schemas import ChannelConfig, ExportJobStatus
from ..utils import utc_now

logger = logging.getLogger(__name__)

# ─── Source identifier ────────────────────────────────────────────────────────

JOB_SOURCE = "daily_archive"


# ─── Timezone helper ──────────────────────────────────────────────────────────

def _get_tz(tz_name: str):
    """Return a ZoneInfo object or None (falls back to UTC)."""
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("[daily-archive] Timezone '%s' not found — falling back to UTC.", tz_name)
            return None
    except ImportError:
        return None


# ─── Output path helpers ──────────────────────────────────────────────────────

def _get_archive_output_path(
    channel_id: str,
    config: ChannelConfig,
    target_date_str: str,
) -> Path:
    """
    Determine the full output file path for a daily archive.

    Filename format: ``{channel.name} {YYYYMMDD} 00-24.mp4``
    The date part is derived from *target_date_str* (``YYYY-MM-DD``).

    Folder priority (first non-empty wins):
    1. ``settings.daily_archive_dir``
    2. ``paths.final_dir``
    3. ``{paths.record_root}/archive``
    4. ``{exports_dir}/{channel_id}/archive``
    """
    settings = get_settings()

    # Build filename: e.g. "RTS1 20260405 00-24.mp4"
    # Convert YYYY-MM-DD → YYYYMMDD
    date_compact = target_date_str.replace("-", "")
    filename = f"{config.name} {date_compact} 00-24.mp4"

    # Determine output folder
    folder: Optional[Path] = None

    if settings.daily_archive_dir:
        folder = Path(settings.daily_archive_dir)
    elif config.paths.final_dir:
        folder = resolve_channel_path(config.paths.final_dir)
    elif config.paths.record_root:
        folder = resolve_channel_path(config.paths.record_root) / "archive"
    else:
        folder = settings.exports_dir / channel_id / "archive"

    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _is_already_archived(
    channel_id: str,
    target_date_str: str,
    db: Session,
) -> bool:
    """
    Return True if a non-failed daily archive job already exists for
    *(channel_id, target_date_str)*.

    Failed jobs are intentionally excluded so the operator can trigger a
    re-run by deleting the failed row.
    """
    existing = (
        db.query(ExportJob)
        .filter(
            ExportJob.channel_id == channel_id,
            ExportJob.date == target_date_str,
            ExportJob.job_source == JOB_SOURCE,
            ExportJob.status != ExportJobStatus.FAILED,
        )
        .first()
    )
    return existing is not None


def _get_segments_for_date(
    channel_id: str,
    target_date_str: str,
    db: Session,
) -> list[SegmentRecord]:
    """
    Return all complete SegmentRecord rows for *(channel_id, target_date_str)*,
    ordered by start_time.

    Uses ``manifest_date`` (local-timezone date string) so timezone-aware
    channels produce correct archives regardless of UTC offset.
    """
    return (
        db.query(SegmentRecord)
        .filter(
            SegmentRecord.channel_id == channel_id,
            SegmentRecord.manifest_date == target_date_str,
            SegmentRecord.status == "complete",
        )
        .order_by(SegmentRecord.start_time)
        .all()
    )


def _create_archive_job(
    channel_id: str,
    target_date_str: str,
    output_path: Path,
    log_path: Path,
    db: Session,
) -> ExportJob:
    """
    Insert a new ExportJob row with ``job_source="daily_archive"`` and
    return it.  Status starts as ``"queued"`` and is updated by the runner.
    """
    job = ExportJob(
        channel_id=channel_id,
        date=target_date_str,
        in_time="00:00:00",
        out_time="23:59:59",
        status=ExportJobStatus.QUEUED,
        progress_percent=0.0,
        output_path=str(output_path),
        log_path=str(log_path),
        has_gaps=False,
        job_source=JOB_SOURCE,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _update_job(db: Session, job_id: int, **kwargs) -> None:
    db.query(ExportJob).filter(ExportJob.id == job_id).update(kwargs)
    db.commit()


# ─── Concat file builder ──────────────────────────────────────────────────────

def _build_daily_archive_concat(
    segments: list[SegmentRecord],
    concat_path: Path,
) -> None:
    """
    Write an ffconcat file listing all *segments* in order without any
    inpoint/outpoint trimming (full segment stream-copy).
    """
    lines = ["ffconcat version 1.0\n"]
    for seg in segments:
        lines.append(f"file '{seg.path}'\n")
    concat_path.write_text("".join(lines), encoding="utf-8")


# ─── FFmpeg runner ────────────────────────────────────────────────────────────

async def _run_archive_ffmpeg(
    job_id: int,
    cmd: list[str],
    log_path: Path,
) -> bool:
    """
    Run *cmd* as an asyncio subprocess, streaming stderr to *log_path*.
    Returns True on exit code 0.

    Progress is not tracked (no ``-progress`` flag) since the total duration
    of a 24-hour archive is hard to estimate in advance when there are gaps.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    SessionLocal = get_session_factory()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log_path.write_text(f"Failed to start FFmpeg: {exc}\n", encoding="utf-8")
        return False

    log_lines: list[str] = []
    try:
        assert proc.stderr is not None
        async for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            log_lines.append(line)
    except asyncio.CancelledError:
        proc.terminate()
        await proc.wait()
        raise

    await proc.wait()
    log_path.write_text("\n".join(log_lines), encoding="utf-8")

    # Update progress to 100 when done
    try:
        with SessionLocal() as db:
            if proc.returncode == 0:
                _update_job(db, job_id, progress_percent=100.0)
    except Exception:
        pass

    return proc.returncode == 0


# ─── Per-channel archive runner ───────────────────────────────────────────────

async def _archive_channel(
    channel_id: str,
    config: ChannelConfig,
    target_date_str: str,
) -> None:
    """
    Create the daily archive for one channel and one calendar day.

    1. Check for an existing non-failed archive job (deduplication).
    2. Query segments from the DB by manifest_date.
    3. Create an ExportJob row (queued).
    4. Build and run the FFmpeg stream-copy concat command.
    5. Update the job to completed/failed.
    """
    settings = get_settings()
    SessionLocal = get_session_factory()

    # ── Deduplication check ────────────────────────────────────────────────
    with SessionLocal() as db:
        if _is_already_archived(channel_id, target_date_str, db):
            logger.debug(
                "[daily-archive][%s] Archive for %s already exists — skipping.",
                channel_id, target_date_str,
            )
            return

        segments = _get_segments_for_date(channel_id, target_date_str, db)

    if not segments:
        logger.info(
            "[daily-archive][%s] No segments found for %s — creating failed job.",
            channel_id, target_date_str,
        )
        # Record a failed job so we don't keep trying until segments appear
        output_path = _get_archive_output_path(channel_id, config, target_date_str)
        log_path = _build_log_path(settings, channel_id, target_date_str)
        with SessionLocal() as db:
            job = _create_archive_job(channel_id, target_date_str, output_path, log_path, db)
            _update_job(
                db, job.id,
                status=ExportJobStatus.FAILED,
                error_message=f"No complete segments found for {channel_id} on {target_date_str}.",
                completed_at=utc_now(),
            )
        return

    output_path = _get_archive_output_path(channel_id, config, target_date_str)
    log_path = _build_log_path(settings, channel_id, target_date_str)

    # ── Create job record ──────────────────────────────────────────────────
    with SessionLocal() as db:
        job = _create_archive_job(channel_id, target_date_str, output_path, log_path, db)
        job_id = job.id

    logger.info(
        "[daily-archive][%s] Archiving %s → %s (%d segments, job_id=%d)",
        channel_id, target_date_str, output_path.name, len(segments), job_id,
    )

    # ── Mark running ───────────────────────────────────────────────────────
    with SessionLocal() as db:
        _update_job(db, job_id, status=ExportJobStatus.RUNNING, started_at=utc_now())

    concat_file: Optional[Path] = None

    try:
        ffmpeg_path = config.ffmpeg_path

        if len(segments) == 1:
            # Single segment: direct stream-copy
            seg = segments[0]
            cmd = [
                ffmpeg_path, "-y",
                "-i", seg.path,
                "-c", "copy",
                str(output_path),
            ]
        else:
            # Multiple segments: concat demuxer
            concat_file = output_path.parent / f".concat_{job_id}.txt"
            _build_daily_archive_concat(segments, concat_file)
            cmd = [
                ffmpeg_path, "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                str(output_path),
            ]

        success = await _run_archive_ffmpeg(job_id, cmd, log_path)

        if not success:
            raise RuntimeError(
                f"FFmpeg stream-copy failed for {channel_id} {target_date_str}. "
                f"See log: {log_path}"
            )

        # ── Mark completed ─────────────────────────────────────────────────
        with SessionLocal() as db:
            _update_job(
                db, job_id,
                status=ExportJobStatus.COMPLETED,
                progress_percent=100.0,
                completed_at=utc_now(),
            )
        logger.info(
            "[daily-archive][%s] Completed archive %s → %s",
            channel_id, target_date_str, output_path,
        )

    except asyncio.CancelledError:
        # Remove partial output on cancellation
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        with SessionLocal() as db:
            _update_job(
                db, job_id,
                status=ExportJobStatus.CANCELLED,
                completed_at=utc_now(),
            )
        raise

    except Exception as exc:
        errmsg = str(exc)
        logger.error("[daily-archive][%s] Failed: %s", channel_id, errmsg)
        # Remove partial output
        if output_path.exists():
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
        with SessionLocal() as db:
            _update_job(
                db, job_id,
                status=ExportJobStatus.FAILED,
                error_message=errmsg,
                completed_at=utc_now(),
            )

    finally:
        if concat_file is not None and concat_file.exists():
            try:
                concat_file.unlink()
            except OSError:
                pass


# ─── Log path helper ──────────────────────────────────────────────────────────

def _build_log_path(settings, channel_id: str, target_date_str: str) -> Path:
    """Return the log file path for a daily archive job."""
    # Use a timestamp suffix to avoid collisions on retry
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = settings.export_logs_dir / channel_id / "archive"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"daily_archive_{target_date_str}_{ts}.log"


# ─── Channel loader ───────────────────────────────────────────────────────────

def _load_archive_channels() -> list[tuple[str, ChannelConfig]]:
    """
    Load all enabled channels configured for daily archiving.

    Returns a list of ``(channel_id, ChannelConfig)`` tuples.
    Channels are filtered by ``daily_archive_channels`` (``"all"`` or a
    comma-separated list of IDs).
    """
    from ..db.models import Channel

    settings = get_settings()
    SessionLocal = get_session_factory()

    # Determine allowed channel IDs (None = all)
    allowed_ids: Optional[set[str]] = None
    raw = settings.daily_archive_channels.strip()
    if raw and raw.lower() != "all":
        allowed_ids = {cid.strip() for cid in raw.split(",") if cid.strip()}

    result: list[tuple[str, ChannelConfig]] = []
    try:
        with SessionLocal() as db:
            channels = db.query(Channel).filter(Channel.enabled.is_(True)).all()
            for ch in channels:
                if allowed_ids is not None and ch.id not in allowed_ids:
                    continue
                try:
                    config = ChannelConfig.model_validate_json(ch.config_json)
                    result.append((ch.id, config))
                except Exception as exc:
                    logger.warning(
                        "[daily-archive] Could not parse config for channel '%s': %s",
                        ch.id, exc,
                    )
    except Exception as exc:
        logger.error("[daily-archive] Failed to load channels: %s", exc)

    return result


# ─── Trigger-time check ───────────────────────────────────────────────────────

def _should_trigger_now(settings) -> bool:
    """
    Return True if the current local time has reached or passed
    ``daily_archive_time`` for today.

    This is a simple HH:MM comparison — the scheduler calls this every 60
    seconds and the archive service itself deduplicates via the DB.
    """
    tz = _get_tz(settings.daily_archive_timezone)
    now_local: datetime
    if tz is not None:
        now_local = datetime.now(tz)
    else:
        now_local = datetime.now(timezone.utc)

    try:
        trigger_h, trigger_m = (int(p) for p in settings.daily_archive_time.split(":"))
    except (ValueError, AttributeError):
        logger.error(
            "[daily-archive] Invalid daily_archive_time '%s'. Expected HH:MM.",
            settings.daily_archive_time,
        )
        return False

    trigger_total = trigger_h * 60 + trigger_m
    now_total = now_local.hour * 60 + now_local.minute
    return now_total >= trigger_total


def _get_target_date_str(settings) -> str:
    """Return the YYYY-MM-DD string for *yesterday* in ``daily_archive_timezone``."""
    tz = _get_tz(settings.daily_archive_timezone)
    if tz is not None:
        now_local = datetime.now(tz)
    else:
        now_local = datetime.now(timezone.utc)
    yesterday = (now_local - timedelta(days=1)).date()
    return yesterday.strftime("%Y-%m-%d")


# ─── Public entry point ───────────────────────────────────────────────────────

async def run_daily_archive() -> None:
    """
    Async entry point called by the scheduler every minute.

    Triggers the daily archive for all configured channels when the current
    local time has reached ``daily_archive_time``.  Each channel is archived
    for the previous calendar day in ``daily_archive_timezone``.

    Deduplication is handled at the DB level: if a non-failed daily archive
    job already exists for ``(channel_id, date)``, that channel is skipped.
    """
    settings = get_settings()

    if not settings.daily_archive_enabled:
        return

    if not _should_trigger_now(settings):
        return

    target_date_str = _get_target_date_str(settings)
    channels = _load_archive_channels()

    if not channels:
        logger.debug("[daily-archive] No channels configured for archiving.")
        return

    logger.info(
        "[daily-archive] Checking %d channel(s) for date %s.",
        len(channels), target_date_str,
    )

    # Run all channels concurrently; errors per channel are caught inside
    await asyncio.gather(
        *[
            _archive_channel(channel_id, config, target_date_str)
            for channel_id, config in channels
        ],
        return_exceptions=True,
    )
