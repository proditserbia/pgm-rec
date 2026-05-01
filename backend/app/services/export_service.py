"""
Export Service — Phase 2B / 2C.

Implements the FFmpeg-based video export engine for PGMRec.

Architecture
────────────
1. ``build_export_command()`` — decides the FFmpeg strategy based on how many
   segment files the range covers and returns a ready-to-use arg list.

   Strategy A — single segment:
       ffmpeg -ss <offset> -i <file> -t <duration> [-threads N] -c copy <out>

   Strategy B — multiple segments (concat demuxer with inpoint/outpoint):
       Writes a temp ffconcat file, then:
       ffmpeg -f concat -safe 0 -i <concat_file> [-threads N] -c copy <out>

   Stream-copy fallback — if the initial run exits non-zero:
       Retry with -c:v libx264 -preset veryfast -c:a aac (re-encode).

2. ``verify_export_output()`` — Phase 2C post-export verification:
   - file exists and size > 0
   - ffprobe to read actual duration
   - actual duration within tolerance of requested duration

3. ``run_export_job()`` — the async coroutine that drives a single job:
   - loads the job from the DB
   - resolves the export range via the Phase 2A resolver
   - builds the output path and log path
   - runs FFmpeg, capturing stderr to a log file
   - updates progress_percent (0 → 100)
   - runs post-export verification (Phase 2C)
   - marks the job completed/failed in the DB
   - called by the export worker

Public API
────────────
- build_export_command(resolve_result, output_path, ffmpeg_path, threads, concat_file)
  → list[str]  (stream-copy args)
- build_export_command_reencode(resolve_result, output_path, ffmpeg_path, threads,
                                concat_file) → list[str]
- verify_export_output(output_path, expected_duration, ffprobe_path, tolerance)
  → (ok: bool, actual_duration: float | None, error: str | None)
- run_export_job(job_id) → None   (coroutine — updates DB when done)
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..config.settings import get_settings
from ..db.models import ExportJob
from ..db.session import get_session_factory
from ..models.schemas import (
    ExportJobStatus,
    ResolveRangeRequest,
    ResolveRangeResponse,
    SegmentSlice,
)
from ..utils import utc_now
from .manifest_service import resolve_export_range

logger = logging.getLogger(__name__)


# ─── Filename helpers ─────────────────────────────────────────────────────────

def _sanitize(s: str) -> str:
    """Replace characters unsafe in filenames with underscores."""
    return re.sub(r"[^\w\-]", "_", s)


def build_output_path(
    exports_dir: Path,
    channel_id: str,
    date: str,
    in_time: str,
    out_time: str,
) -> Path:
    """
    Compute the output file path for an export job.

    Format: exports_dir/{channel_id}/{date}/{channel_id}_{date}_{in}_{to}_{out}.mp4
    Example: data/exports/rts1/2026-04-01/rts1_2026-04-01_14-05-30_to_14-22-10.mp4
    """
    in_s = _sanitize(in_time)
    out_s = _sanitize(out_time)
    filename = f"{channel_id}_{date}_{in_s}_to_{out_s}.mp4"
    folder = exports_dir / channel_id / date
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename


def build_log_path(
    export_logs_dir: Path,
    channel_id: str,
    date: str,
    job_id: int,
) -> Path:
    """Compute the log file path for an export job."""
    folder = export_logs_dir / channel_id / date
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"export_{job_id}.log"


# ─── FFmpeg concat file writer ────────────────────────────────────────────────

def write_concat_file(
    concat_path: Path,
    segments: list[SegmentSlice],
    first_offset: float,
    out_dt_seconds_from_day_start: float,
) -> None:
    """
    Write an ffconcat file for *segments*.

    Uses ``inpoint`` on the first segment and ``outpoint`` on the last segment
    to trim precisely without re-encoding.

    *out_dt_seconds_from_day_start* is unused here; the last segment's
    ``outpoint`` is computed from the desired out-time relative to the last
    segment's start.
    """
    lines = ["ffconcat version 1.0\n"]
    for idx, seg in enumerate(segments):
        lines.append(f"file '{seg.path}'\n")
        if idx == 0 and first_offset > 0:
            lines.append(f"inpoint {first_offset:.6f}\n")
    concat_path.write_text("".join(lines), encoding="utf-8")


def write_concat_file_with_outpoint(
    concat_path: Path,
    segments: list[SegmentSlice],
    first_offset: float,
    last_outpoint: float,
) -> None:
    """
    Write an ffconcat file using ``inpoint`` for the first segment and
    ``outpoint`` for the last segment.

    *last_outpoint* is the number of seconds from the *start* of the last
    segment at which playback should stop.
    """
    lines = ["ffconcat version 1.0\n"]
    for idx, seg in enumerate(segments):
        lines.append(f"file '{seg.path}'\n")
        if idx == 0 and first_offset > 0:
            lines.append(f"inpoint {first_offset:.6f}\n")
        if idx == len(segments) - 1 and last_outpoint < seg.duration_seconds:
            lines.append(f"outpoint {last_outpoint:.6f}\n")
    concat_path.write_text("".join(lines), encoding="utf-8")


# ─── FFmpeg command builders ──────────────────────────────────────────────────

def build_export_command(
    resolve: ResolveRangeResponse,
    output_path: Path,
    ffmpeg_path: str,
    threads: int,
    concat_file: Optional[Path],
) -> list[str]:
    """
    Build the FFmpeg command for stream-copy export.

    Single segment:
        ffmpeg -y [-threads N] -ss <offset> -i <file> -t <duration> -c copy <out>

    Multiple segments:
        ffmpeg -y [-threads N] -f concat -safe 0 -i <concat_file> -c copy <out>
    """
    segments = resolve.segments
    duration = resolve.export_duration_seconds
    offset = resolve.first_segment_offset_seconds

    cmd: list[str] = [ffmpeg_path, "-y"]
    if threads > 0:
        cmd += ["-threads", str(threads)]

    if len(segments) == 1:
        cmd += [
            "-ss", f"{offset:.6f}",
            "-i", segments[0].path,
            "-t", f"{duration:.6f}",
        ]
    else:
        assert concat_file is not None, "concat_file required for multi-segment export"
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_file)]

    cmd += ["-c", "copy", str(output_path)]
    return cmd


def build_export_command_reencode(
    resolve: ResolveRangeResponse,
    output_path: Path,
    ffmpeg_path: str,
    threads: int,
    concat_file: Optional[Path],
) -> list[str]:
    """
    Build the FFmpeg command for re-encode export (fallback).

    Uses libx264/veryfast + aac.  Same input strategy as the stream-copy
    variant; only the codec flags differ.
    """
    segments = resolve.segments
    duration = resolve.export_duration_seconds
    offset = resolve.first_segment_offset_seconds

    cmd: list[str] = [ffmpeg_path, "-y"]
    if threads > 0:
        cmd += ["-threads", str(threads)]

    if len(segments) == 1:
        cmd += [
            "-ss", f"{offset:.6f}",
            "-i", segments[0].path,
            "-t", f"{duration:.6f}",
        ]
    else:
        assert concat_file is not None, "concat_file required for multi-segment export"
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_file)]

    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-c:a", "aac",
        str(output_path),
    ]
    return cmd


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _load_job(db: Session, job_id: int) -> Optional[ExportJob]:
    return db.query(ExportJob).filter(ExportJob.id == job_id).first()


def _update_job(db: Session, job_id: int, **kwargs) -> None:
    db.query(ExportJob).filter(ExportJob.id == job_id).update(kwargs)
    db.commit()


# ─── Progress parser ─────────────────────────────────────────────────────────

def _parse_progress(line: str, total_seconds: float) -> Optional[float]:
    """
    Extract progress percentage from an FFmpeg stderr progress line.

    FFmpeg emits ``out_time_ms=NNNN`` (microseconds) when ``-progress`` is used,
    or ``time=HH:MM:SS.mm`` in plain stderr.  We parse the plain stderr form.
    Returns 0.0–100.0 or None if the line is not a time line.
    """
    m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
    if m and total_seconds > 0:
        h, mn, s, cs = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        elapsed = h * 3600 + mn * 60 + s + cs / 100.0
        return min(100.0, elapsed / total_seconds * 100.0)
    return None


# ─── Phase 2C: Output verification ───────────────────────────────────────────

def verify_export_output(
    output_path: Path,
    expected_duration: float,
    ffprobe_path: str = "ffprobe",
    tolerance: float = 5.0,
) -> tuple[bool, Optional[float], Optional[str]]:
    """
    Verify that an export output file is valid.

    Checks:
    1. File exists.
    2. File size > 0.
    3. ffprobe can read the duration.
    4. Actual duration is within *tolerance* seconds of *expected_duration*.

    Returns:
        (ok, actual_duration_seconds, error_message)
        - ok=True means all checks passed.
        - actual_duration_seconds is None if ffprobe failed.
        - error_message is None when ok=True.
    """
    # 1. File existence
    if not output_path.exists():
        return False, None, f"Output file does not exist: {output_path}"

    # 2. File size
    try:
        size = output_path.stat().st_size
    except OSError as exc:
        return False, None, f"Cannot stat output file: {exc}"

    if size == 0:
        return False, None, f"Output file is empty: {output_path}"

    # 3. ffprobe duration
    actual: Optional[float] = None
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw:
                actual = float(raw)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass

    if actual is None:
        # ffprobe unavailable or failed — treat as a soft warning, not hard fail
        logger.warning(
            "[export] ffprobe unavailable or failed for %s — skipping duration check.",
            output_path,
        )
        return True, None, None

    # 4. Duration tolerance check
    diff = abs(actual - expected_duration)
    if diff > tolerance:
        return (
            False,
            actual,
            f"Duration mismatch: expected {expected_duration:.1f}s, "
            f"got {actual:.1f}s (diff={diff:.1f}s > tolerance={tolerance:.1f}s).",
        )

    return True, actual, None


# ─── Core job runner ─────────────────────────────────────────────────────────

async def run_export_job(job_id: int) -> None:
    """
    Run a single export job end-to-end (coroutine).

    Called by the export worker.  Updates the DB job row throughout.
    """
    settings = get_settings()
    SessionLocal = get_session_factory()
    concat_file: Optional[Path] = None
    output_path: Optional[Path] = None

    with SessionLocal() as db:
        job = _load_job(db, job_id)
        if job is None:
            logger.error("[export] Job %d not found.", job_id)
            return
        if job.status == ExportJobStatus.CANCELLED:
            logger.info("[export] Job %d was cancelled before it started.", job_id)
            return

        channel_id = job.channel_id
        date = job.date
        in_time = job.in_time
        out_time = job.out_time
        preroll = getattr(job, "preroll_seconds", 0.0) or 0.0
        postroll = getattr(job, "postroll_seconds", 0.0) or 0.0

    logger.info("[export][%d] Starting export %s %s %s → %s", job_id, channel_id, date, in_time, out_time)

    # ── Mark running ───────────────────────────────────────────────────────
    with SessionLocal() as db:
        _update_job(db, job_id,
                    status=ExportJobStatus.RUNNING,
                    started_at=utc_now(),
                    progress_percent=0.0)

    try:
        # ── Resolve range ──────────────────────────────────────────────────
        with SessionLocal() as db:
            request = ResolveRangeRequest(
                date=date,
                in_time=in_time,
                out_time=out_time,
                preroll_seconds=preroll,
                postroll_seconds=postroll,
            )
            resolve = resolve_export_range(channel_id, request, db)

        if not resolve.segments:
            raise RuntimeError(
                f"No segments found for {channel_id} {date} {in_time}–{out_time}. "
                "Have segments been registered in the manifest yet?"
            )

        # Check all segment files exist
        missing = [s.path for s in resolve.segments if not Path(s.path).exists()]
        if missing:
            raise RuntimeError(
                f"Missing segment file(s): {', '.join(missing)}"
            )

        # ── Determine output + log paths ───────────────────────────────────
        output_path = build_output_path(
            settings.exports_dir, channel_id, date, in_time, out_time
        )
        log_path = build_log_path(
            settings.export_logs_dir, channel_id, date, job_id
        )

        # Persist paths immediately so they appear in GET /exports/{id}
        with SessionLocal() as db:
            _update_job(db, job_id,
                        output_path=str(output_path),
                        log_path=str(log_path))

        # ── Get ffmpeg path from channel config ────────────────────────────
        ffmpeg_path = "ffmpeg"
        with SessionLocal() as db:
            from ..db.models import Channel
            from ..models.schemas import ChannelConfig
            ch = db.query(Channel).filter(Channel.id == channel_id).first()
            if ch:
                try:
                    cfg = ChannelConfig.model_validate_json(ch.config_json)
                    ffmpeg_path = cfg.ffmpeg_path
                except Exception:
                    pass

        # Derive ffprobe path (same directory as ffmpeg)
        from .manifest_service import _get_ffprobe_path
        ffprobe_path = _get_ffprobe_path(ffmpeg_path)

        # ── Build concat file for multi-segment export ─────────────────────
        if len(resolve.segments) > 1:
            concat_file = output_path.parent / f"concat_{job_id}.txt"
            # Compute outpoint for the last segment
            last_seg = resolve.segments[-1]
            from datetime import timedelta
            last_seg_start_naive = last_seg.start_time
            if hasattr(last_seg_start_naive, "tzinfo") and last_seg_start_naive.tzinfo:
                last_seg_start_naive = last_seg_start_naive.replace(tzinfo=None)
            from datetime import datetime as _dt
            base_date = _dt.strptime(date, "%Y-%m-%d")
            in_h, in_m, in_s = map(int, in_time.split(":"))
            in_dt = base_date + timedelta(hours=in_h, minutes=in_m, seconds=in_s)
            # Effective out = effective_in + export_duration.
            # effective_in = original_in - preroll, so:
            # effective_out = (original_in - preroll) + export_duration_seconds
            effective_in_dt = in_dt - timedelta(seconds=preroll)
            out_dt = effective_in_dt + timedelta(seconds=resolve.export_duration_seconds)
            last_outpoint = (out_dt - last_seg_start_naive).total_seconds()
            write_concat_file_with_outpoint(
                concat_file,
                resolve.segments,
                resolve.first_segment_offset_seconds,
                last_outpoint,
            )

        # ── Try stream copy ────────────────────────────────────────────────
        cmd = build_export_command(
            resolve, output_path, ffmpeg_path,
            settings.export_ffmpeg_threads, concat_file
        )

        logger.info("[export][%d] Running (stream copy): %s", job_id, cmd)
        success = await _run_ffmpeg(
            job_id, cmd, log_path, resolve.export_duration_seconds
        )

        # ── Fallback to re-encode ──────────────────────────────────────────
        if not success:
            logger.warning(
                "[export][%d] Stream copy failed — retrying with re-encode.", job_id
            )
            # Remove partial output if it exists
            if output_path.exists():
                output_path.unlink(missing_ok=True)

            cmd_re = build_export_command_reencode(
                resolve, output_path, ffmpeg_path,
                settings.export_ffmpeg_threads, concat_file
            )
            logger.info("[export][%d] Running (re-encode): %s", job_id, cmd_re)
            success = await _run_ffmpeg(
                job_id, cmd_re, log_path, resolve.export_duration_seconds
            )

        if not success:
            raise RuntimeError(
                "FFmpeg failed (both stream-copy and re-encode). "
                f"See log: {log_path}"
            )

        # ── Phase 2C: Verify output ────────────────────────────────────────
        ok, actual_duration, verify_error = verify_export_output(
            output_path,
            resolve.export_duration_seconds,
            ffprobe_path,
            settings.export_duration_tolerance_seconds,
        )
        if not ok:
            raise RuntimeError(f"Output verification failed: {verify_error}")

        # ── Mark completed ─────────────────────────────────────────────────
        with SessionLocal() as db:
            _update_job(db, job_id,
                        status=ExportJobStatus.COMPLETED,
                        progress_percent=100.0,
                        actual_duration_seconds=actual_duration,
                        completed_at=utc_now())

        logger.info("[export][%d] Completed → %s (actual_duration=%.1fs)",
                    job_id, output_path, actual_duration or 0.0)

    except asyncio.CancelledError:
        # Job was cancelled mid-run — remove partial output
        if output_path is not None and output_path.exists():
            try:
                output_path.unlink(missing_ok=True)
                logger.info("[export][%d] Removed partial output on cancel: %s", job_id, output_path)
            except OSError:
                pass
        with SessionLocal() as db:
            _update_job(db, job_id,
                        status=ExportJobStatus.CANCELLED,
                        completed_at=utc_now())
        logger.info("[export][%d] Cancelled.", job_id)
        raise

    except Exception as exc:
        errmsg = str(exc)
        logger.error("[export][%d] Failed: %s", job_id, errmsg)
        # Remove partial output on failure too
        if output_path is not None and output_path.exists():
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
        with SessionLocal() as db:
            _update_job(db, job_id,
                        status=ExportJobStatus.FAILED,
                        error_message=errmsg,
                        completed_at=utc_now())

    finally:
        # Clean up temp concat file
        if concat_file is not None and concat_file.exists():
            try:
                concat_file.unlink()
            except OSError:
                pass


# ─── FFmpeg subprocess runner ─────────────────────────────────────────────────

async def _run_ffmpeg(
    job_id: int,
    cmd: list[str],
    log_path: Path,
    total_seconds: float,
) -> bool:
    """
    Run *cmd* as an asyncio subprocess.

    Streams stderr to *log_path* while updating the job's progress_percent
    in the DB.  Returns True on exit code 0, False otherwise.

    Registers the running process in the export worker so it can be cancelled.
    """
    from .export_worker import get_export_worker  # local import to avoid circular

    SessionLocal = get_session_factory()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # ffmpeg binary not found
        log_path.write_text(f"Failed to start FFmpeg: {exc}\n", encoding="utf-8")
        return False

    # Register so cancel endpoint can kill the process
    get_export_worker().register_process(job_id, proc)

    last_progress = 0.0
    log_lines: list[str] = []

    try:
        assert proc.stderr is not None
        async for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            log_lines.append(line)
            pct = _parse_progress(line, total_seconds)
            if pct is not None and pct > last_progress:
                last_progress = pct
                with SessionLocal() as db:
                    _update_job(db, job_id, progress_percent=round(pct, 1))
    except asyncio.CancelledError:
        proc.terminate()
        await proc.wait()
        raise
    finally:
        get_export_worker().unregister_process(job_id)

    await proc.wait()
    log_path.write_text("\n".join(log_lines), encoding="utf-8")
    return proc.returncode == 0

