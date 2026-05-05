from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Literal, Optional

from pydantic import model_validator
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

    # File mover (1_record → 2_chunks) — DEPRECATED: use segment_indexer instead.
    # These settings are retained for backward compatibility with legacy channel
    # configs that still use the 1_record/2_chunks pipeline.
    file_mover_interval_seconds: int = 30
    # A file must be at least this many seconds old before it is moved
    # (guards against moving a file FFmpeg is still writing)
    file_mover_min_age_seconds: int = 30
    # Phase 1.6 — double-check: time (seconds) between the two size reads
    file_mover_stability_check_seconds: float = 1.0

    # Phase 23 — Segment Indexer (replaces file_mover for date-folder channels)
    # How often the indexer runs (seconds).
    segment_indexer_interval_seconds: int = 15
    # A file must be at least this many seconds old before it is indexed.
    segment_indexer_min_age_seconds: int = 30
    # Time (seconds) between the two size reads for stability check.
    segment_indexer_stability_check_seconds: float = 1.0
    # Minimum ffprobe duration (seconds) — segments shorter than this are skipped.
    segment_indexer_min_duration_seconds: float = 1.0

    # Phase 24 — Daily Archive Export
    # When enabled, a scheduled job creates a 24-hour archive file for each
    # configured channel once per day, using the manifest DB to concatenate
    # completed segments into a single output file.
    #
    # The archive for a given calendar day is triggered at ``daily_archive_time``
    # (HH:MM) in the ``daily_archive_timezone`` timezone.  The previous calendar
    # day in that timezone is always archived — so a trigger at 00:30 Belgrade
    # time on 2026-04-06 archives 2026-04-05.
    #
    # Output naming:  ``{channel.name} {YYYYMMDD} 00-24.mp4``
    # Output folder priority:
    #   1. ``daily_archive_dir`` (if set)
    #   2. ``paths.final_dir`` of the channel (if configured)
    #   3. ``{paths.record_root}/archive`` (if record_root is configured)
    #   4. ``{exports_dir}/{channel_id}/archive`` (fallback)
    daily_archive_enabled: bool = False
    # Time of day (HH:MM) in daily_archive_timezone when the job triggers.
    daily_archive_time: str = "00:30"
    # "all" or a comma-separated list of channel IDs to include.
    daily_archive_channels: str = "all"
    # IANA timezone name used to determine "yesterday" (should match the
    # channel timezone so manifest_date values align correctly).
    daily_archive_timezone: str = "Europe/Belgrade"
    # Override output directory for all channels.  Empty = auto-detect per channel.
    daily_archive_dir: str = ""

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
    # Phase 9 — How long (seconds) to wait for index.m3u8 to appear after
    # preview start before declaring the preview "failed".
    preview_startup_timeout_seconds: int = 30

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

    # Phase 18 — Channel config source-of-truth mode.
    # Controls how channel configuration is resolved at startup and on each
    # API request.  Set via PGMREC_CHANNEL_CONFIG_MODE in .env or env vars.
    #
    # "db" (default / current behaviour):
    #   JSON seeds the DB once on first startup; the DB is authoritative.
    #   A WARNING is logged if JSON and DB differ; use
    #   POST /channels/{id}/reload-config to apply changes.
    #
    # "json":
    #   Channel config is always read directly from the JSON files on disk.
    #   The DB stores a seed copy but is bypassed for every API request.
    #   Editing a JSON file takes effect immediately — no reload needed.
    #
    # "json_override_db":
    #   On every startup, JSON files automatically overwrite the DB config
    #   for any channels where they differ (equivalent to running
    #   reload-config for all channels automatically on startup).
    channel_config_mode: Literal["db", "json", "json_override_db"] = "db"

    # ── .env / database_url validation ────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_database_url(self) -> "Settings":
        """
        Validate PGMREC_DATABASE_URL at startup.

        1. If the URL points to a SQLite file whose parent directory does not
           exist, create the directory automatically so that the DB can be
           initialised without manual setup.

        2. If the URL contains a Linux-style absolute path (starting with ``/``)
           while running on Windows, emit a CRITICAL warning so the operator
           knows the path will not resolve correctly.
        """
        url = self.database_url

        # ── SQLite: extract the file path ──────────────────────────────────
        sqlite_path: str | None = None
        if url.startswith("sqlite:///"):
            # sqlite:///relative/path  or  sqlite:////abs/path (Unix)
            # sqlite:///C:/Windows/path (Windows — 3 slashes + drive letter)
            sqlite_path = url[len("sqlite:///"):]
        elif url.startswith("sqlite://"):
            # sqlite:///:memory: — no file path
            pass

        if sqlite_path and sqlite_path not in (":memory:", ""):
            db_file = Path(sqlite_path)
            parent = db_file.parent
            if not parent.exists():
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                    _logger.info(
                        "Config: created missing SQLite parent directory: %s", parent
                    )
                except OSError as exc:
                    raise ValueError(
                        f"PGMREC_DATABASE_URL points to '{db_file}' but the parent "
                        f"directory '{parent}' does not exist and could not be created: {exc}"
                    ) from exc

            # ── Windows + Linux path check ─────────────────────────────────
            if sys.platform == "win32" and db_file.as_posix().startswith("/"):
                _logger.critical(
                    "⚠️  CONFIG ERROR: PGMREC_DATABASE_URL contains a Linux-style path "
                    "('%s') but you are running on Windows.  "
                    "Use a Windows path like 'sqlite:///C:/pgmrec/pgmrec.db' instead.",
                    sqlite_path,
                )

        return self


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


def resolve_date_folder(
    record_root: str,
    date_folder_format: str = "%Y_%m_%d",
    date=None,
) -> Path:
    """
    Resolve the date-based sub-folder path for a channel under *record_root*.

    Returns ``{record_root}/{date_str}/`` where ``date_str`` is produced by
    applying *date_folder_format* (strftime) to *date*.  When *date* is
    ``None`` today's date is used.

    The directory is **not** created here — call ``.mkdir(parents=True,
    exist_ok=True)`` on the returned path if creation is needed.

    Parameters
    ----------
    record_root : str
        Channel recording root path (resolved via
        :func:`resolve_channel_path`).
    date_folder_format : str
        strftime pattern for the sub-folder name (default ``"%Y_%m_%d"``).
    date : datetime.date | None
        Target date; uses today when ``None``.
    """
    import datetime as _dt

    if date is None:
        date = _dt.date.today()
    folder_name = date.strftime(date_folder_format)
    root = resolve_channel_path(record_root)
    return root / folder_name
