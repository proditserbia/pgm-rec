"""
Pydantic schemas for channel configuration and API responses.

ChannelConfig is the single source of truth for FFmpeg command generation.
Every field maps directly to a parameter extracted from record_rts1.bat.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ─── Channel configuration ────────────────────────────────────────────────────

class OverlayConfig(BaseModel):
    """drawtext filter configuration (maps to record_rts1.bat WATERMARK variable)."""

    enabled: bool = True
    fontsize: int = 13
    fontcolor: str = "black"
    box: bool = True
    boxcolor: str = "white@0.4"
    # Platform-specific font paths; builder selects correct one at runtime
    fontfile_win: str = "C:\\Windows\\Fonts\\verdana.ttf"
    fontfile_linux: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    # strftime-compatible format string (no FFmpeg escaping here — builder handles that)
    time_format: str = "%d-%m-%y %H:%M:%S"
    x: str = "(w-tw)/30"
    y: str = "(h-th)/20"


class FilterConfig(BaseModel):
    """Video filter chain configuration."""

    deinterlace: bool = True
    scale_width: int = 1024
    scale_height: int = 576
    overlay: OverlayConfig = Field(default_factory=OverlayConfig)


class CaptureConfig(BaseModel):
    """Input device configuration (maps to -f / -s / -framerate / -i in bat)."""

    # dshow on Windows (Decklink), v4l2 on Linux
    device_type: str = "dshow"
    video_device: str = "Decklink Video Capture"
    audio_device: str = "Decklink Audio Capture"
    resolution: str = "720x576"
    framerate: int = 25


class EncodingConfig(BaseModel):
    """Video and audio encoding parameters."""

    video_codec: str = "libx264"
    preset: str = "veryfast"
    video_bitrate: str = "1500k"
    audio_bitrate: str = "128k"


class SegmentConfig(BaseModel):
    """stream_segment muxer configuration."""

    segment_time: str = "00:05:00"
    segment_atclocktime: bool = True
    reset_timestamps: bool = True
    strftime: bool = True
    # strftime-compatible filename pattern (no extension)
    filename_pattern: str = "%d%m%y-%H%M%S"


class PathConfig(BaseModel):
    """Three-stage output directory pipeline (replicates bat folder convention)."""

    record_dir: str   # Stage 1: active recording  (1_record)
    chunks_dir: str   # Stage 2: completed chunks  (2_chunks)
    final_dir: str    # Stage 3: merged daily files (3_final)


class RetentionConfig(BaseModel):
    """File retention / cleanup policy."""

    enabled: bool = True
    days: int = 30


class PreviewConfig(BaseModel):
    """Preview stream configuration (rts1_preview.bat)."""

    enabled: bool = False
    port: int = 23001
    scale: str = "320:180"
    fps: int = 5


class ChannelConfig(BaseModel):
    """
    Complete channel configuration — source of truth for all FFmpeg operations.

    Stored as JSON in the DB; loaded at runtime and passed to the command builder.
    Designed to be multi-channel from day one (no RTS1-specific hardcoding here).
    """

    id: str                  # Unique slug, e.g. "rts1"
    name: str                # Short name, e.g. "RTS1"
    display_name: str        # Human label, e.g. "RTS1 - PRVI PROGRAM"
    enabled: bool = True
    ffmpeg_path: str = "ffmpeg"   # Full path or executable name on PATH
    # IANA timezone name for the recording machine's local clock.
    # Used when interpreting segment filenames and writing manifests.
    timezone: str = "Europe/Belgrade"

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    encoding: EncodingConfig = Field(default_factory=EncodingConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    segmentation: SegmentConfig = Field(default_factory=SegmentConfig)
    paths: PathConfig
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    preview: PreviewConfig = Field(default_factory=PreviewConfig)


# ─── Process / health status ──────────────────────────────────────────────────

class ProcessStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"   # operating but repeated anomalies / restarts
    COOLDOWN = "cooldown"   # too many restarts — auto-restart temporarily paused
    UNKNOWN = "unknown"


# ─── API response models ───────────────────────────────────────────────────────

class ChannelStatusResponse(BaseModel):
    channel_id: str
    channel_name: str
    status: ProcessStatus
    health: HealthStatus = HealthStatus.UNKNOWN
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    uptime_seconds: Optional[float] = None
    last_seen_alive: Optional[datetime] = None
    log_path: Optional[str] = None


class ChannelSummary(BaseModel):
    id: str
    name: str
    display_name: str
    enabled: bool
    status: ProcessStatus
    health: HealthStatus = HealthStatus.UNKNOWN
    pid: Optional[int] = None


class ChannelDetailResponse(BaseModel):
    summary: ChannelSummary
    config: ChannelConfig
    status: ChannelStatusResponse


class ActionResponse(BaseModel):
    success: bool
    message: str
    channel_id: str
    status: ProcessStatus


class LogsResponse(BaseModel):
    channel_id: str
    log_path: Optional[str] = None
    lines: list[str]


class CommandPreviewResponse(BaseModel):
    channel_id: str
    command: list[str]
    command_str: str


class ProcessHistoryEntry(BaseModel):
    id: int
    pid: Optional[int]
    status: str
    started_at: Optional[datetime]
    stopped_at: Optional[datetime]
    exit_code: Optional[int]
    log_path: Optional[str]
    adopted: bool = False


# ─── Monitoring response models ───────────────────────────────────────────────

class WatchdogEventResponse(BaseModel):
    id: int
    channel_id: str
    event_type: str
    detected_at: datetime
    details: Optional[str] = None


class SegmentAnomalyResponse(BaseModel):
    id: int
    channel_id: str
    detected_at: datetime
    last_segment_time: Optional[datetime]
    expected_interval_seconds: float
    actual_gap_seconds: float
    resolved: bool


class ChannelHealthResponse(BaseModel):
    channel_id: str
    channel_name: str
    status: ProcessStatus
    health: HealthStatus
    pid: Optional[int] = None
    last_seen_alive: Optional[datetime] = None
    recent_events: list[WatchdogEventResponse] = Field(default_factory=list)


class SystemHealthResponse(BaseModel):
    channels: list[ChannelHealthResponse]
    total: int
    running: int
    healthy: int
    unhealthy: int
    degraded: int
    cooldown: int
    unknown: int


class ChannelDebugResponse(BaseModel):
    """Detailed real-time diagnostics for a single channel — Phase 1.6."""

    channel_id: str
    health: HealthStatus
    pid: Optional[int] = None
    # Restart history
    last_restart_time: Optional[datetime] = None
    restart_count_window: int = 0
    cooldown_remaining_seconds: float = 0.0
    # Segment / file monitoring
    last_segment_time: Optional[datetime] = None
    last_file_size: Optional[int] = None
    last_file_size_change_at: Optional[datetime] = None
    stall_seconds: Optional[float] = None  # seconds since last file size growth


# ─── Preview response models — Phase 2 ───────────────────────────────────────

class PreviewHealth(str, Enum):
    HEALTHY = "healthy"
    DOWN = "down"
    UNKNOWN = "unknown"


class PreviewStatusResponse(BaseModel):
    """Live status of the preview process for one channel."""

    channel_id: str
    running: bool
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    stream_url: Optional[str] = None
    health: PreviewHealth = PreviewHealth.UNKNOWN


# ─── Manifest / Export Index models — Phase 2A ───────────────────────────────

class SegmentStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    ERROR = "error"


class SegmentEntry(BaseModel):
    """One recorded segment as stored in the daily JSON manifest and DB."""

    filename: str
    path: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    size_bytes: int
    status: SegmentStatus = SegmentStatus.COMPLETE
    created_at: datetime
    ffprobe_verified: bool = False


class GapEntry(BaseModel):
    """A detected gap between two consecutive segments."""

    gap_start: datetime
    gap_end: datetime
    gap_seconds: float


class DailyManifest(BaseModel):
    """
    Per-channel, per-day recording manifest (JSON source of truth).

    Written to: data/manifests/{channel_id}/{YYYY-MM-DD}.json

    Human-readable and hand-repairable.  The DB indexes the same data for
    fast API queries; the JSON file is always canonical.
    """

    channel_id: str
    date: str               # YYYY-MM-DD in the channel's local timezone
    timezone: str           # IANA timezone name
    segment_duration_target: int  # seconds (normally 300 = 5 min)
    segments: list[SegmentEntry] = Field(default_factory=list)
    gaps: list[GapEntry] = Field(default_factory=list)
    updated_at: datetime


class ResolveRangeRequest(BaseModel):
    """Input to the export range resolver."""

    date: str      # YYYY-MM-DD
    in_time: str   # HH:MM:SS
    out_time: str  # HH:MM:SS


class SegmentSlice(BaseModel):
    """A segment reference returned by the export range resolver."""

    filename: str
    path: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float


class ResolveRangeResponse(BaseModel):
    """
    Result of resolving an export time range.

    Tells the caller exactly which segment files are needed, where to trim
    the first and last segments, and whether there are any gaps.
    """

    channel_id: str
    date: str
    in_time: str
    out_time: str
    segments: list[SegmentSlice]
    first_segment_offset_seconds: float
    export_duration_seconds: float
    has_gaps: bool
    gaps: list[GapEntry]
