from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).parent.parent.parent.resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PGMREC_",
        env_file=".env",
        env_file_encoding="utf-8",
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
    jwt_secret_key: str = "change-me-in-production-pgmrec-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480   # 8 hours

    # Admin seed: created once on first startup if no users exist.
    # Override via PGMREC_ADMIN_USERNAME / PGMREC_ADMIN_PASSWORD env vars.
    admin_username: str = "admin"
    admin_password: str = "pgmrec-admin"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
