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
    scale: str = "300:-1"


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
    unknown: int
