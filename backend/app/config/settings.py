from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).parent.parent.parent.resolve()  # backend/
_PROJECT_ROOT = _BASE_DIR.parent                            # repository root

_logger = logging.getLogger(__name__)


def _find_env_file() -> Path | None:
    """
    Search for a .env file in priority order:
      1. backend/.env  — same directory as the Python source (preferred)
      2. project root/.env — useful when running from repository root or as a
         Windows service whose CWD is not the backend directory

    Returns the first file that actually exists, or None if neither is found.
    Pydantic-settings will then fall back to environment variables only.
    """
    for candidate in (_BASE_DIR / ".env", _PROJECT_ROOT / ".env"):
        if candidate.is_file():
            return candidate
    return None


_ENV_FILE: Path | None = _find_env_file()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PGMREC_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PGMRec"
    app_version: str = "0.1.0"
    debug: bool = False

    # Paths — all overridable via PGMREC_* env vars
    base_dir: Path = _BASE_DIR
    data_dir: Path = _BASE_DIR / "data"
    logs_dir: Path = _BASE_DIR / "logs"
    channels_config_dir: Path = _BASE_DIR / "data" / "channels"

    # Database
    database_url: str = f"sqlite:///{_BASE_DIR}/pgmrec.db"

    # Process control
    stop_timeout_seconds: int = 15

    # Watchdog
    # How often the watchdog loop runs (seconds)
    watchdog_interval_seconds: int = 10
    # Grace period added on top of segment_time before declaring stale output
    watchdog_segment_tolerance_seconds: int = 30

    # Phase 1.6 — FFmpeg hang / stall detection
    # If the newest segment file's size hasn't grown for this many seconds, the
    # recording is considered stalled (process alive but producing no output).
    stall_detection_seconds: int = 60

    # Phase 1.6 — Restart backoff / cooldown
    # Maximum auto-restarts allowed within restart_backoff_window_seconds before
    # the channel enters COOLDOWN and auto-restart is temporarily disabled.
    restart_backoff_max_restarts: int = 5
    # Sliding window (seconds) within which restarts are counted.
    restart_backoff_window_seconds: int = 300   # 5 minutes
    # How long (seconds) a channel stays in COOLDOWN before it can be restarted.
    restart_cooldown_seconds: int = 120         # 2 minutes
    # Small buffer between stop and start during auto-restart.
    restart_pre_delay_seconds: float = 2.0

    # File mover (1_record → 2_chunks)
    file_mover_interval_seconds: int = 30
    # A file must be at least this many seconds old before it is moved
    # (guards against moving a file FFmpeg is still writing)
    file_mover_min_age_seconds: int = 30
    # Phase 1.6 — double-check: time (seconds) between the two size reads
    file_mover_stability_check_seconds: float = 1.0

    # Retention cleaner
    retention_run_interval_seconds: int = 3600  # once per hour

    # Phase 6.2 — Event table pruning
    # Watchdog events and segment anomalies older than this many days are deleted
    # by the retention scheduler (0 = disabled).
    event_retention_days: int = 90

    # Log management
    # Maximum number of log files to keep per channel (oldest are deleted)
    log_max_files_per_channel: int = 30

    # Phase 2A — Recording Manifest & Export Index Layer
    # Root directory for per-channel daily JSON manifests
    manifests_dir: Path = _BASE_DIR / "data" / "manifests"
    # IANA timezone name used when interpreting segment filenames (the recording
    # machine's local clock is assumed to be in this timezone).
    manifest_timezone: str = "Europe/Belgrade"
    # Gaps smaller than this threshold (seconds) are silently ignored.
    manifest_gap_tolerance_seconds: float = 10.0

    # Phase 2B — Export Engine
    # Root directory for exported video files
    exports_dir: Path = _BASE_DIR / "data" / "exports"
    # Root directory for per-job export FFmpeg logs
    export_logs_dir: Path = _BASE_DIR / "logs" / "exports"
    # Maximum number of export jobs that may run concurrently
    max_concurrent_exports: int = 2
    # Number of threads FFmpeg may use per export job (0 = auto)
    export_ffmpeg_threads: int = 0

    # Phase 2C — Export Hardening
    # Delete exported files and logs older than this many days (0 = disabled)
    export_retention_days: int = 30
    # Reject export requests whose duration exceeds this many seconds (0 = unlimited)
    max_export_duration_seconds: int = 7200   # 2 hours
    # Acceptable difference (seconds) between requested and verified actual duration
    export_duration_tolerance_seconds: float = 5.0

    # Phase 4 — Authentication & RBAC
    # HS256 secret used to sign JWTs.  Override in production via PGMREC_JWT_SECRET_KEY.
    jwt_secret_key: str = "change-me-before-starting-pgmrec-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480   # 8 hours

    # Admin seed: created once on first startup if no users exist.
    # Override via PGMREC_ADMIN_USERNAME / PGMREC_ADMIN_PASSWORD env vars.
    admin_username: str = "admin"
    admin_password: str = "pgmrec-admin"

    # Phase 5 — HLS Browser Preview
    # Root directory for per-channel HLS output (index.m3u8 + .ts segments)
    preview_dir: Path = _BASE_DIR / "data" / "preview"

    # Phase 6 — Deployment
    # Network bind address.  Used by the startup scripts (start.sh, service files).
    # 127.0.0.1 — local machine only (default, safe)
    # 0.0.0.0   — listen on all interfaces (required for LAN access)
    # Override via PGMREC_HOST env var or in .env
    host: str = "127.0.0.1"
    # TCP port to listen on.  Override via PGMREC_PORT env var or in .env
    port: int = 8000

    # Comma-separated allowed CORS origins.
    # PGMRec is a LAN-only application — list every browser origin that needs
    # to reach the API (scheme + host + port must all match).
    #
    # Default covers:
    #   - React dev server (Vite):  http://localhost:5173 / http://127.0.0.1:5173
    #   - Embedded production UI:   http://localhost:8000 / http://127.0.0.1:8000
    #
    # LAN access — add your server's LAN IP, e.g.:
    #   PGMREC_CORS_ORIGINS=http://192.168.1.50:8000
    # or append to the defaults:
    #   PGMREC_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.50:8000
    cors_origins: str = (
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:8000,"
        "http://127.0.0.1:8000"
    )

    # Absolute path to ffmpeg binary.  Overrides per-channel ffmpeg_path if set.
    # If empty, per-channel config is used (default).
    ffmpeg_path_override: str = ""
    # Absolute path to ffprobe binary.  Used by manifest/export services.
    ffprobe_path: str = "ffprobe"

    # Phase 6.2 — Disk safety
    # Minimum free disk bytes required before starting/restarting FFmpeg recording.
    # Default: 500 MB.  Set to 0 to disable the check.
    min_free_disk_bytes: int = 500 * 1024 * 1024  # 500 MB

    # Phase 7 — Alert system (broadcast alert classification + debounce)
    # Each alert type can be independently enabled/disabled and given a debounce
    # delay (trigger_after_seconds).  A condition must persist for at least that
    # many seconds before a WatchdogEvent is logged.  Set trigger_after_seconds=0
    # to fire immediately.
    #
    # loss_of_recording: FFmpeg process died or no new segment files (severity=2)
    alert_loss_of_recording_enabled: bool = True
    alert_loss_of_recording_trigger_after_seconds: int = 0   # fire immediately
    # freeze: video frame frozen / image unchanged (severity=2; detection TBD)
    alert_freeze_enabled: bool = True
    alert_freeze_trigger_after_seconds: int = 40
    # silence: audio below threshold (severity=1; detection TBD)
    alert_silence_enabled: bool = True
    alert_silence_trigger_after_seconds: int = 10
    # black: pure black frame / no video signal (severity=2; detection TBD)
    alert_black_enabled: bool = True
    alert_black_trigger_after_seconds: int = 10

    # Phase 8 — Recording root for portable relative channel paths.
    # When set, relative paths in channel JSON configs (paths.record_dir,
    # paths.chunks_dir, paths.final_dir) are resolved under this directory.
    # Absolute paths in channel JSON are always used as-is.
    #
    # Example: PGMREC_RECORDING_ROOT=D:\AutoRec\record
    # Channel JSON can then use: "paths": {"record_dir": "rts1/1_record", ...}
    # Effective path becomes: D:\AutoRec\record\rts1\1_record
    recording_root: Optional[Path] = None


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_loaded_env_file() -> Path | None:
    """Return the absolute path of the .env file loaded at startup, or None."""
    return _ENV_FILE


def resolve_channel_path(path_str: str) -> Path:
    """
    Resolve a channel recording path string to an absolute Path.

    - Absolute paths are returned as-is.
    - Relative paths are resolved under ``PGMREC_RECORDING_ROOT`` when that
      setting is configured, enabling portable channel JSON configs such as::

          "paths": {"record_dir": "rts1/1_record", ...}

    - If the path is relative and ``recording_root`` is not set, it is resolved
      relative to the current working directory and a warning is emitted.
    """
    p = Path(path_str)
    if p.is_absolute():
        return p
    settings = get_settings()
    if settings.recording_root is not None:
        return (settings.recording_root / p).resolve()
    _logger.warning(
        "Channel path '%s' is relative but PGMREC_RECORDING_ROOT is not set; "
        "resolving relative to CWD (%s). "
        "Set PGMREC_RECORDING_ROOT in .env for predictable paths.",
        path_str,
        Path.cwd(),
    )
    return p.resolve()
