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

    # File mover (1_record → 2_chunks)
    file_mover_interval_seconds: int = 30
    # A file must be at least this many seconds old before it is moved
    # (guards against moving a file FFmpeg is still writing)
    file_mover_min_age_seconds: int = 30

    # Retention cleaner
    retention_run_interval_seconds: int = 3600  # once per hour

    # Log management
    # Maximum number of log files to keep per channel (oldest are deleted)
    log_max_files_per_channel: int = 30


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
