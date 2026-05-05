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
    """
    Input device configuration — Phase 11: fully configurable per channel.

    Maps to the FFmpeg input flags (-f / -video_size / -framerate / -i etc.).

    dshow (Windows Decklink):
        -f dshow -video_size <resolution> -framerate <fps>
        [-pixel_format <fmt>] [-vcodec <codec>]
        -i video=<video_device>:audio=<audio_device>

    v4l2 / generic (Linux):
        -f <device_type> -s <resolution> -framerate <fps>
        [-pixel_format <fmt>] [-vcodec <codec>]
        -i <video_device>

    Fields:
        device_type   : FFmpeg demuxer name (``dshow``, ``v4l2``, ``avfoundation``…)
        video_device  : Video capture device name / path
        audio_device  : Audio capture device name (dshow only; ignored for v4l2)
        resolution    : Frame size in ``WxH`` format, e.g. ``1920x1080``
        framerate     : Capture frame rate in fps
        pixel_format  : Optional pixel format override, e.g. ``uyvy422``, ``nv12``
                        (passed as -pixel_format to the demuxer, before -i)
        vcodec        : Optional forced input video codec, e.g. ``rawvideo``
                        (passed as -vcodec to the demuxer, before -i; rarely needed)
    """

    # dshow on Windows (Decklink), v4l2 on Linux
    device_type: str = "dshow"
    video_device: str = "Decklink Video Capture"
    audio_device: str = "Decklink Audio Capture"
    resolution: str = "720x576"
    framerate: int = 25
    # Phase 11 — optional per-channel capture format overrides
    pixel_format: Optional[str] = None   # e.g. "uyvy422", "nv12"
    vcodec: Optional[str] = None         # e.g. "rawvideo" (rarely needed)


class RecordingPreviewOutputConfig(BaseModel):
    """
    Configuration for a secondary low-res preview output embedded inside
    the recording FFmpeg process — Phase 12.

    When enabled, ``build_ffmpeg_command()`` uses ``-filter_complex`` to split
    the video pipeline and write a low-resolution preview stream alongside the
    normal segment recording.

    Phase 22 — ``mode`` field selects the preview output format:

    - ``"udp"`` (default): send preview stream to a UDP endpoint so a separate
      HLS FFmpeg process can receive and remux it.  Legacy two-process design.
      Requires ``recording_preview_output.send_url`` / ``listen_url`` to be set.
    - ``"hls_direct"``: write HLS preview files **directly** from the recording
      FFmpeg process to ``data/preview/{channel_id}/``.  No second FFmpeg
      process is needed.  This is the recommended mode on Windows because it
      eliminates the fragile UDP → HLS remux timing chain.
    - ``"disabled"``: recording FFmpeg produces no preview output even when
      ``enabled=True``.  Useful to temporarily suppress preview without
      removing the config block.

    ⚠️  SAFETY: This output runs inside the **same** FFmpeg process as recording.
    A bad codec configuration (e.g. ``h264_nvenc`` unavailable) will crash the
    recording process.

    Guidance:
    - ``fail_safe_mode=True`` (default): logs a prominent WARNING when NVENC is
      requested so operators know the risk.  Does NOT suppress NVENC; set
      ``video_codec="libx264"`` if you want guaranteed-safe CPU encoding.
    - ``fallback_to_cpu=True``: if FFmpeg exits immediately after start with an
      NVENC-related error, ``ProcessManager.start()`` retries once using
      ``video_codec="libx264"`` for the preview output.  Main recording
      settings (``encoding.*``) are never modified by the fallback.

    UDP URL split — Phase 14:
    On Windows the sender and receiver must NOT share the same URL string because
    the FFmpeg UDP sender and the HLS-preview receiver each need different socket
    options:

    - ``send_url``   (recording output / sender):
        ``udp://127.0.0.1:<port>?pkt_size=1316``
    - ``listen_url`` (HLS preview input / receiver):
        ``udp://127.0.0.1:<port>?overrun_nonfatal=1&fifo_size=50000000``

    Only one process may bind to a given UDP port at a time.  Do NOT run
    ffplay and the HLS preview process concurrently on the same port.  If you
    get ``bind failed: Error number -10048``, stop all existing ffmpeg/ffplay
    processes that hold that port and retry.

    If ``send_url`` / ``listen_url`` are omitted, ``url`` is used as a fallback
    for both roles (backward-compatible behaviour from Phase 12).
    """

    enabled: bool = False
    # Phase 22 — output mode: "udp" | "hls_direct" | "disabled"
    # Default "udp" preserves backward compatibility with Phase 12–21 configs.
    mode: str = "udp"
    # Legacy single-URL field — kept for backward compatibility.
    # If send_url / listen_url are not set, url is used for both roles.
    url: str = "udp://127.0.0.1:23001?pkt_size=1316"
    # Phase 14 — explicit sender URL used by the recording FFmpeg output.
    # Example: "udp://127.0.0.1:23001?pkt_size=1316"
    send_url: Optional[str] = None
    # Phase 14 — explicit listener URL used by the HLS preview FFmpeg input.
    # Example: "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"
    listen_url: Optional[str] = None
    format: str = "mpegts"

    # ── Video ──────────────────────────────────────────────────────────────────
    # Use "h264_nvenc" to request NVENC encoding (see safety note above).
    video_codec: str = "libx264"
    # Preset — libx264: e.g. "veryfast"; NVENC: e.g. "p1" (low-latency fast)
    preset: Optional[str] = "veryfast"
    # Tune — NVENC only: e.g. "ull" (ultra-low latency); ignored for libx264
    tune: Optional[str] = None
    width: int = 480
    height: int = 270
    fps: int = 10
    bitrate: str = "400k"

    # ── Audio ──────────────────────────────────────────────────────────────────
    audio_enabled: bool = False
    audio_codec: str = "aac"
    audio_bitrate: str = "96k"
    audio_sample_rate: int = 48000

    # ── Safety ─────────────────────────────────────────────────────────────────
    # When True (default): emit a WARNING log if NVENC is configured, reminding
    # the operator that a codec failure inside recording will stop recording.
    fail_safe_mode: bool = True
    # When True: if FFmpeg exits immediately after start with an NVENC-related
    # error, ProcessManager.start() will retry once using video_codec='libx264'
    # for the preview output.  Main recording settings are never changed.
    fallback_to_cpu: bool = False
    # Optional explicit output pixel format for the main recording stream.
    # Example: "yuv420p" for broader browser/player compatibility.
    # When None (default) the pixel format is determined by the encoder;
    # do not set this unless a specific format is required.
    pixel_format_output: Optional[str] = None

    # ── HLS direct output settings (used when mode == "hls_direct") ───────────
    # Phase 22 — controls the HLS muxer when mode="hls_direct".
    # Each HLS segment duration in seconds.
    hls_time: int = 2
    # Number of segments to keep in the playlist (older ones are deleted).
    hls_list_size: int = 5
    # HLS muxer flags:
    #   delete_segments    — remove old .ts files when they fall off the list
    #   append_list        — append to existing playlist rather than rewriting
    #   independent_segments — each segment starts with a keyframe (seekable)
    hls_flags: str = "delete_segments+append_list+independent_segments"


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
    """
    Recording output path configuration.

    **New (Phase 23) — date-folder mode** (preferred):
        Set ``record_root`` to the channel recording root, e.g.
        ``D:\\AutoRec\\record\\rts1``.  FFmpeg writes segments directly into
        a per-day sub-folder:  ``{record_root}/{YYYY_MM_DD}/{filename}.mp4``

        ``use_date_folders`` is ``True`` by default when ``record_root`` is
        set (and ``record_dir`` is absent).  The date sub-folder name format
        is controlled by ``date_folder_format`` (strftime; default
        ``%Y_%m_%d``).

    **Legacy (Phase 1–22) — three-stage pipeline**:
        ``record_dir`` / ``chunks_dir`` / ``final_dir`` remain accepted for
        backward compatibility.  When present and ``record_root`` is absent,
        ``use_date_folders`` defaults to ``False`` and the old file-mover
        behaviour is retained.
    """

    # ── New (Phase 23) ─────────────────────────────────────────────────────
    # Root directory for the channel (e.g. D:\AutoRec\record\rts1).
    # FFmpeg writes segments into {record_root}/{date_folder}/{filename}.mp4
    record_root: Optional[str] = None

    # When True segments are written into date-based sub-folders.
    # Defaults to True when record_root is set; False when using legacy paths.
    use_date_folders: Optional[bool] = None

    # strftime pattern for the date sub-folder name (default: "%Y_%m_%d").
    date_folder_format: str = "%Y_%m_%d"

    # ── Legacy (Phase 1–22) ────────────────────────────────────────────────
    record_dir: Optional[str] = None   # Stage 1: active recording  (1_record)
    chunks_dir: Optional[str] = None   # Stage 2: completed chunks  (2_chunks)
    final_dir: Optional[str] = None    # Stage 3: merged daily files (3_final)

    @property
    def effective_use_date_folders(self) -> bool:
        """Return True when date-folder mode should be used."""
        if self.use_date_folders is not None:
            return self.use_date_folders
        # Auto-detect: date folders whenever record_root is set.
        # record_dir may coexist for backward compatibility, but record_root wins.
        return self.record_root is not None


class RetentionConfig(BaseModel):
    """File retention / cleanup policy."""

    enabled: bool = True
    days: int = 30


class PreviewConfig(BaseModel):
    """
    Preview stream configuration — Phase 5: HLS.

    HLS fields were added in Phase 5; legacy MJPEG fields are kept for
    backward-compatibility with older JSON configs.
    """

    enabled: bool = False
    # ── Legacy MJPEG fields (Phase 2 — kept for JSON backward compat) ─────
    port: int = 23001
    scale: str = "320:180"
    fps: int = 5
    # ── HLS fields (Phase 5) ───────────────────────────────────────────────
    width: int = 480
    height: int = 270
    hls_fps: int = 10
    video_bitrate: str = "400k"
    encoder: str = "libx264"
    segment_time: int = 2
    list_size: int = 5
    # Phase 9/10/12/22 — capture input mode for preview.
    # direct_capture       : open the same hardware device as recording (default).
    #   Works when the hardware supports concurrent access (e.g. some v4l2
    #   drivers).  On single-input Blackmagic Decklink systems this WILL
    #   FAIL because the recording process already owns the device.
    # from_recording_output: read completed segment files from record_dir /
    #   chunks_dir instead of opening the device.  The preview is ~one segment
    #   behind live (default segment_time = 5 min) but never contends for the
    #   device.  This is the recommended mode for single-Decklink setups.
    # from_udp             : read from the UDP preview stream produced by the
    #   recording FFmpeg process (via recording_preview_output).  Near-live
    #   monitoring with audio; requires recording to be running with
    #   recording_preview_output.enabled=True and mode="udp".
    # hls_direct           : the recording FFmpeg process writes HLS preview
    #   files directly (recording_preview_output.mode="hls_direct").  No
    #   separate HLS FFmpeg process is started; the preview manager simply
    #   monitors the files produced by the recording process.  This is the
    #   recommended mode on Windows — most robust, no UDP port contention.
    # disabled             : preview is explicitly disabled — start attempts
    #   return 409.
    input_mode: str = "direct_capture"
    # Phase 12 — informational hint: if from_udp mode fails, callers may fall
    # back to from_recording_output automatically.
    fallback_to_cpu: bool = False
    # Phase 14 — per-channel UDP port for from_udp mode.
    # Assign a unique port per channel so multiple channels can stream preview
    # UDP simultaneously without port conflicts:
    #   RTS1 → 23001, RTS2 → 23002, RTS3 → 23003, RTS_TEST → 23004
    # Only one FFmpeg/ffplay listener may bind to a given port at a time.
    # This field is informational metadata; the authoritative URLs are
    # recording_preview_output.send_url and recording_preview_output.listen_url.
    udp_port: Optional[int] = None
    # Phase 17 — HLS generation mode for from_udp input_mode.
    # "copy"      : remux only (-c:v copy -c:a copy); fastest, no CPU encoding.
    #               Requires the UDP stream to be H.264+AAC MPEG-TS.
    # "transcode" : always re-encode to libx264/aac; slower but works with any
    #               source codec.
    # "auto"      : try copy first; if no playlist appears within
    #               preview_startup_timeout_seconds, restart in transcode mode.
    # Default: "auto"
    hls_mode: str = "auto"


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
    # Phase 12 — optional in-process UDP preview output embedded in recording.
    # When set and enabled=True, build_ffmpeg_command() adds a second low-res
    # output alongside the main recording using -filter_complex.
    recording_preview_output: Optional[RecordingPreviewOutputConfig] = None


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
    # Phase 7 — broadcast alert classification
    alert_type: Optional[str] = None
    severity: int = 0


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


class ChannelDiagnosticsResponse(BaseModel):
    """
    Deep diagnostics for a channel — Phase 9.

    Intended for admin troubleshooting of black video, device issues, etc.
    All fields are best-effort; None indicates the value could not be determined.
    """

    channel_id: str
    # Recording command (same as /command endpoint)
    ffmpeg_command: str
    ffmpeg_command_list: list[str]
    # Capture input details from config
    device_type: str
    input_specifier: str
    resolution: str
    framerate: int
    # Record directory (today's date folder for date-based layout, or legacy record_dir)
    record_dir: Optional[str] = None
    # Latest segment on disk (in record_dir)
    latest_segment_path: Optional[str] = None
    latest_segment_size_bytes: Optional[int] = None
    latest_segment_mtime: Optional[datetime] = None
    # Last N lines of recording stderr
    stderr_tail: list[str] = Field(default_factory=list)
    # Hint for listing capture devices on Windows/dshow
    dshow_device_hint: str = (
        'ffmpeg -list_devices true -f dshow -i dummy  '
        '(run on the recording machine)'
    )
    # Phase 17 — manual diagnostic command for from_udp mode (None for other modes).
    # Example: 'ffplay "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"'
    ffplay_hint: Optional[str] = None


# ─── Config reload response ───────────────────────────────────────────────────

class ConfigReloadResponse(BaseModel):
    """Response returned by POST /channels/{id}/reload-config."""

    channel_id: str
    # True if the DB config was actually replaced (JSON differed from DB).
    # False if they were already identical (no-op).
    config_changed: bool
    message: str
    config: ChannelConfig


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


class HlsPreviewStatusResponse(BaseModel):
    """Live status of the HLS preview process for one channel — Phase 5."""

    channel_id: str
    running: bool
    pid: Optional[int] = None
    started_at: Optional[datetime] = None
    playlist_url: Optional[str] = None
    health: PreviewHealth = PreviewHealth.UNKNOWN
    # Phase 9 additions — playlist readiness and startup lifecycle
    # startup_status: "stopped" | "starting" | "running" | "failed"
    startup_status: str = "stopped"
    playlist_ready: bool = False
    stderr_tail: list[str] = Field(default_factory=list)
    failed_reason: Optional[str] = None


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
    # Phase 7 — segment flags (schema preparation; detection not yet implemented)
    never_expires: bool = False
    has_freeze: Optional[bool] = None
    has_silence: Optional[bool] = None


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
    # Phase 7 — pre/post roll wrap (seconds added before/after the requested range)
    preroll_seconds: float = 0.0
    postroll_seconds: float = 0.0


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
    # Phase 7 — effective range after applying pre/post roll (None when no wrap)
    effective_in_time: Optional[str] = None
    effective_out_time: Optional[str] = None


# ─── Export Engine models — Phase 2B ─────────────────────────────────────────

class ExportJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExportJobRequest(BaseModel):
    """Request body for POST /channels/{id}/exports."""

    date: str     # YYYY-MM-DD
    in_time: str  # HH:MM:SS  (UTC)
    out_time: str # HH:MM:SS  (UTC)
    # If True, create the job even when gaps are detected in the resolved range
    allow_gaps: bool = True
    # Phase 7 — pre/post roll wrap (non-negative seconds; default 0 = no wrap)
    preroll_seconds: float = 0.0
    postroll_seconds: float = 0.0
    # Phase 7 — if True, retention cleanup will skip this job's output file
    never_expires: bool = False


class ExportJobResponse(BaseModel):
    """API representation of an ExportJob row."""

    id: int
    channel_id: str
    date: str
    in_time: str
    out_time: str
    status: ExportJobStatus
    progress_percent: float
    output_path: Optional[str] = None
    log_path: Optional[str] = None
    error_message: Optional[str] = None
    has_gaps: bool
    actual_duration_seconds: Optional[float] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    # Phase 7
    preroll_seconds: float = 0.0
    postroll_seconds: float = 0.0
    never_expires: bool = False
    # Phase 24 — "manual" | "daily_archive"
    job_source: str = "manual"


# ─── System — Phase 3.5 ───────────────────────────────────────────────────────

class DiskUsageResponse(BaseModel):
    """Disk usage for the filesystem where recordings are stored."""

    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float


# ─── Recording Retention — Phase 25 ─────────────────────────────────────────

class RetentionRunRequest(BaseModel):
    """
    Request body for POST /api/v1/retention/run.

    channel_id: specific channel to clean up; None = run for all channels.
    dry_run: when True, return what *would* be deleted without actually deleting.
    """

    channel_id: Optional[str] = None
    dry_run: bool = True


class RetentionChannelResult(BaseModel):
    """
    Retention result for a single channel.

    Included in RetentionRunResponse.channels.
    """

    channel_id: str
    skipped: bool = False
    skip_reason: Optional[str] = None
    files_deleted: int = 0
    folders_deleted: int = 0
    total_bytes: int = 0
    # Paths that were (or would be) deleted — populated in both dry_run and live modes.
    files_to_delete: list[str] = Field(default_factory=list)
    folders_to_delete: list[str] = Field(default_factory=list)


class RetentionRunResponse(BaseModel):
    """
    Response body for POST /api/v1/retention/run.

    dry_run=True  → executed=False: files_to_delete/folders_to_delete are populated
                   but nothing was actually deleted.
    dry_run=False → executed=True: deletion was performed; counts reflect actuals.
    """

    dry_run: bool
    executed: bool
    channels: list[RetentionChannelResult] = Field(default_factory=list)
    total_files_deleted: int = 0
    total_folders_deleted: int = 0
    total_bytes: int = 0


# ─── System config — Phase 8 ─────────────────────────────────────────────────

class SystemConfigResponse(BaseModel):
    """Sanitized effective configuration — GET /api/v1/system/config (admin only)."""

    env_file: Optional[str] = None          # which .env was loaded (None = not found)
    data_dir: str
    ffmpeg_path: str                         # override value or "(per-channel config)"
    ffprobe_path: str
    database_url: str                        # password masked
    exports_dir: str
    preview_dir: str
    manifests_dir: str
    cors_origins: list[str]
    host: str
    port: int
    recording_root: Optional[str] = None    # None = not configured
