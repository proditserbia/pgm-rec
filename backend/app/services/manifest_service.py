"""
Recording Manifest Service — Phase 2A.

Implements the ACTUS-inspired JSON manifest layer for PGMRec.

Architecture
────────────
Every time the file_mover promotes a completed segment from
``1_record`` → ``2_chunks``, this service:

1. Parses the segment start time from the filename (using the channel's
   configured strftime pattern and IANA timezone).
2. Calls ffprobe to verify the actual duration (falls back to the configured
   segment_time if ffprobe is unavailable or fails).
3. Writes / updates the per-channel daily manifest JSON:
       data/manifests/{channel_id}/{YYYY-MM-DD}.json
4. Upserts a SegmentRecord row in the DB (secondary index for fast queries).
5. Recomputes and persists gap entries for the day.

Manifest JSON is the source of truth for exports.
The DB (SegmentRecord / ManifestGap) is a queryable index.

Public API
────────────
- parse_segment_start_time(filename, pattern, tz_name) → datetime | None
- register_segment(channel_id, dest_path, config, db)   → SegmentRecord | None
- load_manifest(channel_id, date_str, manifests_dir)    → DailyManifest | None
- resolve_export_range(channel_id, request, db)         → ResolveRangeResponse
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..config.settings import get_settings
from ..db.models import ManifestGap, SegmentRecord
from ..models.schemas import (
    ChannelConfig,
    DailyManifest,
    GapEntry,
    ResolveRangeRequest,
    ResolveRangeResponse,
    SegmentEntry,
    SegmentSlice,
    SegmentStatus,
)

logger = logging.getLogger(__name__)


# ─── Timezone helpers ─────────────────────────────────────────────────────────

def _get_timezone(tz_name: str):
    """
    Return a :class:`zoneinfo.ZoneInfo` for *tz_name*, or ``None`` if the
    timezone database is unavailable (falls back to treating times as UTC).
    """
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("Timezone '%s' not found — falling back to UTC.", tz_name)
            return None
    except ImportError:
        # Python < 3.9 (shouldn't happen but be defensive)
        return None


def _localize(naive_dt: datetime, tz_name: str) -> datetime:
    """
    Attach *tz_name* timezone info to a naive *naive_dt* and convert to UTC.

    If the timezone cannot be loaded, the naive datetime is treated as UTC.
    """
    tz = _get_timezone(tz_name)
    if tz is not None:
        return naive_dt.replace(tzinfo=tz).astimezone(timezone.utc)
    return naive_dt.replace(tzinfo=timezone.utc)


def _to_local_date_str(utc_dt: datetime, tz_name: str) -> str:
    """
    Return the YYYY-MM-DD string for *utc_dt* expressed in *tz_name*.

    Used to decide which daily manifest file a segment belongs to.
    Falls back to UTC date if the timezone cannot be loaded.
    """
    tz = _get_timezone(tz_name)
    if tz is not None:
        local_dt = utc_dt.astimezone(tz)
    else:
        local_dt = utc_dt
    return local_dt.strftime("%Y-%m-%d")


# ─── Filename → start-time parser ────────────────────────────────────────────

def parse_segment_start_time(
    filename: str, pattern: str, tz_name: str
) -> Optional[datetime]:
    """
    Derive the UTC start time from a segment *filename*.

    *pattern* is the strftime pattern configured for the channel (e.g.
    ``%d%m%y-%H%M%S``).  The stem (filename without extension) is parsed
    against this pattern.  The resulting naive datetime is treated as being
    in *tz_name* and then converted to UTC.

    Returns ``None`` if parsing fails (unexpected filename format).
    """
    stem = Path(filename).stem
    try:
        naive = datetime.strptime(stem, pattern)
    except ValueError:
        logger.debug(
            "Cannot parse start time from '%s' using pattern '%s'.", filename, pattern
        )
        return None
    return _localize(naive, tz_name)


# ─── ffprobe integration ──────────────────────────────────────────────────────

def _get_ffprobe_path(ffmpeg_path: str) -> str:
    """Derive the ffprobe binary path from the configured ffmpeg path."""
    p = Path(ffmpeg_path)
    name = p.name
    if name.lower() in ("ffmpeg", "ffmpeg.exe"):
        probe_name = name.lower().replace("ffmpeg", "ffprobe")
        return str(p.parent / probe_name)
    return "ffprobe"


def ffprobe_duration(file_path: Path, ffprobe_path: str = "ffprobe") -> Optional[float]:
    """
    Run ffprobe to get the actual duration (seconds) of *file_path*.

    Returns ``None`` if ffprobe is not installed, the file is unreadable, or
    the output cannot be parsed.  Callers should fall back to the configured
    segment_time in that case.
    """
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
            if raw:
                return float(raw)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


# ─── Manifest JSON I/O ────────────────────────────────────────────────────────

def _manifest_path(channel_id: str, date_str: str, manifests_dir: Path) -> Path:
    return manifests_dir / channel_id / f"{date_str}.json"


def _segment_duration_target_seconds(segment_time: str) -> int:
    """Convert HH:MM:SS segment_time string to integer seconds."""
    try:
        parts = segment_time.split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 300  # 5 minutes default


def load_manifest(
    channel_id: str, date_str: str, manifests_dir: Path
) -> Optional[DailyManifest]:
    """
    Load and parse an existing daily manifest JSON from disk.

    Returns ``None`` if the file does not exist or cannot be parsed.
    """
    path = _manifest_path(channel_id, date_str, manifests_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DailyManifest.model_validate(data)
    except Exception as exc:
        logger.error(
            "[manifest][%s] Failed to load manifest %s: %s", channel_id, path, exc
        )
        return None


def save_manifest(manifest: DailyManifest, manifests_dir: Path) -> None:
    """
    Atomically write *manifest* to its JSON file.

    Uses write-to-temp-then-rename so a crash mid-write never corrupts the
    existing file.
    """
    path = _manifest_path(manifest.channel_id, manifest.date, manifests_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = manifest.model_dump(mode="json")
    # Write to a sibling temp file, then atomically rename
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(path)
    logger.debug("[manifest] Saved %s", path)


# ─── Gap detection ─────────────────────────────────────────────────────────────

def _compute_gaps(
    segments: list[SegmentEntry], tolerance_seconds: float
) -> list[GapEntry]:
    """
    Compute gaps between consecutive segments.

    A gap exists when ``seg[n+1].start_time - seg[n].end_time > tolerance``.
    """
    gaps: list[GapEntry] = []
    sorted_segs = sorted(segments, key=lambda s: s.start_time)
    for i in range(len(sorted_segs) - 1):
        current_end = sorted_segs[i].end_time
        next_start = sorted_segs[i + 1].start_time
        gap_secs = (next_start - current_end).total_seconds()
        if gap_secs > tolerance_seconds:
            gaps.append(GapEntry(
                gap_start=current_end,
                gap_end=next_start,
                gap_seconds=gap_secs,
            ))
    return gaps


def _sync_gaps_to_db(
    channel_id: str,
    date_str: str,
    gaps: list[GapEntry],
    db: Session,
) -> None:
    """
    Replace all ManifestGap rows for *(channel_id, date_str)* with *gaps*.

    Runs inside the caller's DB session (caller commits).
    """
    db.query(ManifestGap).filter(
        ManifestGap.channel_id == channel_id,
        ManifestGap.manifest_date == date_str,
    ).delete(synchronize_session=False)

    for gap in gaps:
        db.add(ManifestGap(
            channel_id=channel_id,
            manifest_date=date_str,
            gap_start=gap.gap_start,
            gap_end=gap.gap_end,
            gap_seconds=gap.gap_seconds,
        ))


# ─── Core: register a segment ─────────────────────────────────────────────────

def register_segment(
    channel_id: str,
    dest_path: Path,
    config: ChannelConfig,
    db: Session,
) -> Optional[SegmentRecord]:
    """
    Register a newly moved segment file into the manifest and DB.

    Called by the file_mover after successfully moving a file from
    ``1_record`` → ``2_chunks``.

    Steps:
    1. Parse start time from filename.
    2. Determine duration via ffprobe (or fall back to config segment_time).
    3. Compute end_time.
    4. Derive the local-timezone date string for the manifest partition.
    5. Upsert SegmentRecord in DB.
    6. Load (or create) the DailyManifest, add the segment, recompute gaps.
    7. Save manifest JSON to disk.
    8. Sync gaps to DB.
    9. Commit DB.

    Returns the DB record on success, ``None`` on failure.
    """
    settings = get_settings()
    filename = dest_path.name

    # ── 1. Parse start time ────────────────────────────────────────────────
    start_time = parse_segment_start_time(
        filename,
        config.segmentation.filename_pattern,
        config.timezone,
    )
    if start_time is None:
        logger.warning(
            "[manifest][%s] Cannot parse start time from '%s' — skipping registration.",
            channel_id, filename,
        )
        return None

    # ── 2. Determine duration (ffprobe → fallback) ─────────────────────────
    ffprobe_path = _get_ffprobe_path(config.ffmpeg_path)
    duration = ffprobe_duration(dest_path, ffprobe_path)
    ffprobe_verified = duration is not None
    if duration is None:
        duration = float(_segment_duration_target_seconds(config.segmentation.segment_time))
        logger.debug(
            "[manifest][%s] ffprobe unavailable for '%s' — using configured segment_time (%.0fs).",
            channel_id, filename, duration,
        )

    # ── 3. Compute end_time ────────────────────────────────────────────────
    end_time = start_time + timedelta(seconds=duration)

    # ── 4. Manifest date (local timezone) ─────────────────────────────────
    date_str = _to_local_date_str(start_time, config.timezone)

    # ── 5. File metadata ───────────────────────────────────────────────────
    try:
        size_bytes = dest_path.stat().st_size
    except OSError:
        size_bytes = 0

    now_utc = datetime.now(timezone.utc)

    # ── 6. Upsert SegmentRecord in DB ──────────────────────────────────────
    db_record = (
        db.query(SegmentRecord)
        .filter(
            SegmentRecord.channel_id == channel_id,
            SegmentRecord.filename == filename,
        )
        .first()
    )
    if db_record is None:
        db_record = SegmentRecord(
            channel_id=channel_id,
            filename=filename,
            path=str(dest_path),
            start_time=start_time,
            end_time=end_time,
            duration_seconds=duration,
            size_bytes=size_bytes,
            status=SegmentStatus.COMPLETE.value,
            ffprobe_verified=ffprobe_verified,
            manifest_date=date_str,
        )
        db.add(db_record)
    else:
        db_record.path = str(dest_path)
        db_record.start_time = start_time
        db_record.end_time = end_time
        db_record.duration_seconds = duration
        db_record.size_bytes = size_bytes
        db_record.status = SegmentStatus.COMPLETE.value
        db_record.ffprobe_verified = ffprobe_verified
        db_record.manifest_date = date_str

    # ── 7. Load / create DailyManifest ────────────────────────────────────
    manifests_dir = settings.manifests_dir
    manifest = load_manifest(channel_id, date_str, manifests_dir)
    if manifest is None:
        manifest = DailyManifest(
            channel_id=channel_id,
            date=date_str,
            timezone=config.timezone,
            segment_duration_target=_segment_duration_target_seconds(
                config.segmentation.segment_time
            ),
            segments=[],
            gaps=[],
            updated_at=now_utc,
        )

    # ── 8. Add / replace segment entry in manifest ─────────────────────────
    new_entry = SegmentEntry(
        filename=filename,
        path=str(dest_path),
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration,
        size_bytes=size_bytes,
        status=SegmentStatus.COMPLETE,
        created_at=now_utc,
        ffprobe_verified=ffprobe_verified,
    )
    manifest.segments = [s for s in manifest.segments if s.filename != filename]
    manifest.segments.append(new_entry)
    manifest.segments.sort(key=lambda s: s.start_time)

    # ── 9. Recompute gaps ──────────────────────────────────────────────────
    gaps = _compute_gaps(manifest.segments, settings.manifest_gap_tolerance_seconds)
    manifest.gaps = gaps
    manifest.updated_at = now_utc

    # ── 10. Save manifest JSON ─────────────────────────────────────────────
    try:
        save_manifest(manifest, manifests_dir)
    except Exception as exc:
        logger.error(
            "[manifest][%s] Failed to save manifest for %s: %s", channel_id, date_str, exc
        )

    # ── 11. Sync gaps to DB ────────────────────────────────────────────────
    _sync_gaps_to_db(channel_id, date_str, gaps, db)

    db.commit()

    logger.info(
        "[manifest][%s] Registered '%s': start=%s duration=%.1fs ffprobe=%s gaps=%d",
        channel_id, filename, start_time.isoformat(), duration, ffprobe_verified, len(gaps),
    )
    return db_record


# ─── Export range resolver ────────────────────────────────────────────────────

def resolve_export_range(
    channel_id: str,
    request: ResolveRangeRequest,
    db: Session,
) -> ResolveRangeResponse:
    """
    Resolve a time range to a list of segment files.

    Given a *date* (YYYY-MM-DD), *in_time* (HH:MM:SS), and *out_time* (HH:MM:SS)
    in the **UTC** timezone (the DB stores times in UTC), this function:

    1. Builds UTC datetimes for in and out times on the given date.
    2. Queries the DB for segments that overlap [in_time, out_time].
    3. Computes:
       - first_segment_offset_seconds: how far into the first segment the
         in_time falls.
       - export_duration_seconds: total wall-clock length of the export.
    4. Detects gaps within the resolved range.

    Does NOT perform any actual video processing.
    """
    settings = get_settings()

    # Parse the date + times into naive UTC datetimes.
    # SQLite stores datetimes without timezone info (naive UTC).
    # We use naive UTC throughout to ensure consistent comparisons with DB values.
    try:
        base_date = datetime.strptime(request.date, "%Y-%m-%d")  # naive UTC
        in_h, in_m, in_s = map(int, request.in_time.split(":"))
        out_h, out_m, out_s = map(int, request.out_time.split(":"))
        in_dt = base_date + timedelta(hours=in_h, minutes=in_m, seconds=in_s)
        out_dt = base_date + timedelta(hours=out_h, minutes=out_m, seconds=out_s)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid date/time format: {exc}") from exc

    if out_dt <= in_dt:
        raise ValueError("out_time must be after in_time")

    export_duration = (out_dt - in_dt).total_seconds()

    # Query segments that overlap the requested range:
    #   seg.end_time > in_dt  AND  seg.start_time < out_dt
    db_segments = (
        db.query(SegmentRecord)
        .filter(
            SegmentRecord.channel_id == channel_id,
            SegmentRecord.end_time > in_dt,
            SegmentRecord.start_time < out_dt,
        )
        .order_by(SegmentRecord.start_time)
        .all()
    )

    slices = [
        SegmentSlice(
            filename=seg.filename,
            path=seg.path,
            start_time=seg.start_time,
            end_time=seg.end_time,
            duration_seconds=seg.duration_seconds,
        )
        for seg in db_segments
    ]

    # First segment offset: how many seconds into the first segment is in_dt?
    first_offset = 0.0
    if slices:
        first_offset = max(0.0, (in_dt - slices[0].start_time).total_seconds())

    # Detect gaps within the resolved range
    gaps_in_range: list[GapEntry] = []
    for i in range(len(slices) - 1):
        gap_secs = (slices[i + 1].start_time - slices[i].end_time).total_seconds()
        if gap_secs > settings.manifest_gap_tolerance_seconds:
            gaps_in_range.append(GapEntry(
                gap_start=slices[i].end_time,
                gap_end=slices[i + 1].start_time,
                gap_seconds=gap_secs,
            ))

    return ResolveRangeResponse(
        channel_id=channel_id,
        date=request.date,
        in_time=request.in_time,
        out_time=request.out_time,
        segments=slices,
        first_segment_offset_seconds=first_offset,
        export_duration_seconds=export_duration,
        has_gaps=bool(gaps_in_range),
        gaps=gaps_in_range,
    )
