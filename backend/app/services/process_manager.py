"""
Process manager for PGMRec FFmpeg recording processes.

Manages the full lifecycle of one FFmpeg process per channel:
  - start    — Popen with PID tracking, stderr → log file
  - stop     — SIGTERM → timeout → SIGKILL
  - restart  — stop + pre-delay + start
  - status   — live poll of the OS process
  - log tail — read last N lines from the active log file

Phase 1.6 additions:
  - Stall tracking: last_file_path / last_file_size / last_size_change_at per ProcessInfo
  - Restart backoff: sliding-window restart counter + COOLDOWN state per channel
  - DEGRADED / COOLDOWN health states
  - Pre-delay between stop and start during auto-restart

Phase 6.2 additions:
  - Disk space check before starting FFmpeg (PGMREC_MIN_FREE_DISK_BYTES).
  - Restart history persisted to DB (restart_history table) so backoff
    counters survive server restarts.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..config.settings import get_settings, resolve_channel_path
from ..db.models import ProcessRecord, RestartHistoryRecord
from ..models.schemas import ChannelConfig, HealthStatus, ProcessStatus
from ..utils import utc_now
from .ffmpeg_builder import build_ffmpeg_command, format_command_for_log

logger = logging.getLogger(__name__)


# ─── OS helpers ───────────────────────────────────────────────────────────────

def _pid_exists(pid: int) -> bool:
    """Return True if the OS reports the PID is still running."""
    try:
        if sys.platform == "win32":
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, 0, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False


def _tail_file(path: Path, lines: int) -> list[str]:
    """Return the last *lines* lines of *path* without loading the whole file."""
    if not path.exists():
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            if size == 0:
                return []
            block = min(8192, size)
            buf = b""
            pos = size
            while pos > 0:
                read = min(block, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
                parts = buf.split(b"\n")
                if len(parts) > lines + 1:
                    break
            result = buf.split(b"\n")
            tail = result[-lines:] if len(result) > lines else result
            return [line.decode("utf-8", errors="replace").rstrip() for line in tail if line]
    except OSError as exc:
        logger.warning("Cannot read log %s: %s", path, exc)
        return []


def _wait_for_pid_death(pid: int, timeout: int) -> None:
    """Poll until *pid* is gone or *timeout* seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.25)


# ─── In-memory process state ──────────────────────────────────────────────────

@dataclass
class ProcessInfo:
    """
    Runtime state for one active channel recording process.

    Two creation paths:
    1. Fresh start (process is set): we own the Popen handle.
    2. Adopted orphan (process is None): we only have the PID from a previous run.

    Phase 1.6 additions:
    - Stall detection fields: track the newest segment file's size across watchdog
      cycles.  If the size stops changing for stall_detection_seconds, the
      recording is considered stalled.
    """

    channel_id: str
    pid: int
    log_path: Path
    started_at: datetime
    process: Optional[subprocess.Popen] = field(default=None)
    last_seen_alive: datetime = field(init=False)
    health: HealthStatus = field(default=HealthStatus.UNKNOWN)

    # ── Stall detection state ─────────────────────────────────────────────
    # Path of the segment file we are currently tracking size for
    _stall_tracked_path: Optional[str] = field(default=None, repr=False)
    # Size (bytes) at the last watchdog cycle
    _stall_last_size: Optional[int] = field(default=None, repr=False)
    # When the size last actually changed
    _stall_last_size_change_at: Optional[datetime] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.last_seen_alive = utc_now()

    def is_alive(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        return _pid_exists(self.pid)

    def exit_code(self) -> Optional[int]:
        if self.process is not None:
            return self.process.returncode
        return None

    def mark_alive(self) -> None:
        """Called by the watchdog each time the process is confirmed alive."""
        self.last_seen_alive = utc_now()
        if self.health not in (HealthStatus.DEGRADED, HealthStatus.COOLDOWN):
            self.health = HealthStatus.HEALTHY

    def mark_unhealthy(self) -> None:
        self.health = HealthStatus.UNHEALTHY

    def mark_degraded(self) -> None:
        self.health = HealthStatus.DEGRADED

    def update_stall_tracking(self, file_path: str, current_size: int) -> bool:
        """
        Update stall-detection state for *file_path* / *current_size*.

        Returns True if the size grew (or the tracked file changed), False if
        it appears to be stalled (no size growth since last call).
        """
        now = utc_now()

        # New file started — reset tracking
        if file_path != self._stall_tracked_path:
            self._stall_tracked_path = file_path
            self._stall_last_size = current_size
            self._stall_last_size_change_at = now
            return True  # file just changed → not stalled

        # Same file — check growth
        if current_size != self._stall_last_size:
            self._stall_last_size = current_size
            self._stall_last_size_change_at = now
            return True  # growing → not stalled

        return False  # size unchanged

    @property
    def stall_seconds(self) -> Optional[float]:
        """Seconds since the tracked file last grew, or None if unknown."""
        if self._stall_last_size_change_at is None:
            return None
        return (utc_now() - self._stall_last_size_change_at).total_seconds()

    @property
    def last_file_size(self) -> Optional[int]:
        return self._stall_last_size

    @property
    def last_file_size_change_at(self) -> Optional[datetime]:
        return self._stall_last_size_change_at


# ─── Restart backoff tracker ──────────────────────────────────────────────────

@dataclass
class _RestartHistory:
    """Per-channel restart rate-limiting state."""

    _timestamps: deque = field(default_factory=lambda: deque(maxlen=100))
    _cooldown_until: Optional[datetime] = field(default=None)

    def record_attempt(self) -> None:
        self._timestamps.append(utc_now())

    def count_in_window(self, window_seconds: float) -> int:
        cutoff = utc_now().timestamp() - window_seconds
        return sum(1 for ts in self._timestamps if ts.timestamp() >= cutoff)

    def last_restart_time(self) -> Optional[datetime]:
        return self._timestamps[-1] if self._timestamps else None

    def is_in_cooldown(self) -> bool:
        if self._cooldown_until is None:
            return False
        return utc_now() < self._cooldown_until

    def cooldown_remaining_seconds(self) -> float:
        if self._cooldown_until is None:
            return 0.0
        remaining = (self._cooldown_until - utc_now()).total_seconds()
        return max(0.0, remaining)

    def enter_cooldown(self, seconds: float) -> None:
        from datetime import timedelta
        self._cooldown_until = utc_now() + timedelta(seconds=seconds)

    def exit_cooldown(self) -> None:
        self._cooldown_until = None


# ─── NVENC fallback helpers ───────────────────────────────────────────────────

# How long to wait (seconds) after launching FFmpeg to detect an immediate
# NVENC initialisation failure.  Typical NVENC failures occur within <1 s;
# 3 s is a safe margin that still keeps the start() call reasonably fast.
_NVENC_CRASH_WAIT: float = 3.0

# Keywords that, when present in the FFmpeg stderr log, indicate an NVENC
# driver/device failure rather than a recording or I/O error.
_NVENC_ERROR_KEYWORDS: tuple[str, ...] = ("nvenc", "nvcuda")


def _is_nvenc_failure(log_path: Path) -> bool:
    """
    Return True if *log_path* contains NVENC-related error keywords.

    Used by the NVENC fallback logic in ProcessManager.start() to distinguish
    an NVENC initialisation crash from other immediate FFmpeg exits.
    """
    tail = _tail_file(log_path, 50)
    for line in tail:
        lower = line.lower()
        if any(kw in lower for kw in _NVENC_ERROR_KEYWORDS):
            return True
    return False


# ─── Process manager ──────────────────────────────────────────────────────────

class ProcessManager:
    """
    Singleton that owns all FFmpeg subprocesses for every channel.

    Thread safety: FastAPI runs in an async event loop (single thread by default),
    so simple dict operations are safe. For multi-worker deployments, move state
    to the DB or Redis.
    """

    def __init__(self) -> None:
        self._procs: dict[str, ProcessInfo] = {}
        # Restart history survives process restarts within a server session
        self._restart_history: dict[str, _RestartHistory] = {}

    def _get_or_create_history(self, channel_id: str) -> _RestartHistory:
        if channel_id not in self._restart_history:
            self._restart_history[channel_id] = _RestartHistory()
        return self._restart_history[channel_id]

    # ── Internal helpers ──────────────────────────────────────────────────

    def _reap_if_dead(self, channel_id: str) -> None:
        """Remove a channel entry if its process has already exited."""
        info = self._procs.get(channel_id)
        if info and not info.is_alive():
            logger.warning(
                "[%s] Process PID %d exited unexpectedly (code=%s).",
                channel_id,
                info.pid,
                info.exit_code(),
            )
            del self._procs[channel_id]

    def _latest_log_for(self, channel_id: str) -> Optional[Path]:
        """Find the most recent log file for a channel (used when not running)."""
        settings = get_settings()
        log_dir = settings.logs_dir / "channels" / channel_id
        if not log_dir.exists():
            return None
        files = sorted(log_dir.glob("ffmpeg-*.log"))
        return files[-1] if files else None

    def _new_log_path(self, channel_id: str) -> Path:
        """Create a timestamped log path and ensure the directory exists."""
        settings = get_settings()
        log_dir = settings.logs_dir / "channels" / channel_id
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = utc_now().strftime("%Y%m%d-%H%M%S")
        return log_dir / f"ffmpeg-{ts}.log"

    def _prune_old_logs(self, channel_id: str) -> None:
        """Delete oldest log files when the per-channel limit is exceeded."""
        settings = get_settings()
        log_dir = settings.logs_dir / "channels" / channel_id
        if not log_dir.exists():
            return
        files = sorted(log_dir.glob("ffmpeg-*.log"))
        limit = settings.log_max_files_per_channel
        excess = files[: max(0, len(files) - limit)]
        for f in excess:
            try:
                f.unlink()
                logger.debug("[%s] Pruned old log: %s", channel_id, f.name)
            except OSError as exc:
                logger.warning("[%s] Could not delete old log %s: %s", channel_id, f, exc)

    # ── Public interface ──────────────────────────────────────────────────

    def is_running(self, channel_id: str) -> bool:
        self._reap_if_dead(channel_id)
        return channel_id in self._procs

    def get_status(self, channel_id: str) -> ProcessStatus:
        return ProcessStatus.RUNNING if self.is_running(channel_id) else ProcessStatus.STOPPED

    def get_health(self, channel_id: str) -> HealthStatus:
        # COOLDOWN is a channel-level state that persists even if not running
        hist = self._restart_history.get(channel_id)
        if hist and hist.is_in_cooldown():
            return HealthStatus.COOLDOWN
        info = self._procs.get(channel_id)
        if info is None:
            return HealthStatus.UNKNOWN
        return info.health

    def get_pid(self, channel_id: str) -> Optional[int]:
        self._reap_if_dead(channel_id)
        info = self._procs.get(channel_id)
        return info.pid if info else None

    def get_started_at(self, channel_id: str) -> Optional[datetime]:
        self._reap_if_dead(channel_id)
        info = self._procs.get(channel_id)
        return info.started_at if info else None

    def get_last_seen_alive(self, channel_id: str) -> Optional[datetime]:
        info = self._procs.get(channel_id)
        return info.last_seen_alive if info else None

    def get_log_path(self, channel_id: str) -> Optional[Path]:
        info = self._procs.get(channel_id)
        if info:
            return info.log_path
        return self._latest_log_for(channel_id)

    def mark_alive(self, channel_id: str) -> None:
        """Called by the watchdog to update the liveness timestamp and health."""
        info = self._procs.get(channel_id)
        if info:
            info.mark_alive()

    def mark_unhealthy(self, channel_id: str) -> None:
        info = self._procs.get(channel_id)
        if info:
            info.mark_unhealthy()

    def mark_degraded(self, channel_id: str) -> None:
        info = self._procs.get(channel_id)
        if info:
            info.mark_degraded()

    # ── Stall tracking ────────────────────────────────────────────────────

    def update_stall_tracking(
        self, channel_id: str, file_path: str, current_size: int
    ) -> bool:
        """
        Update stall-detection state.  Returns True if the file is growing
        (healthy), False if stalled.
        """
        info = self._procs.get(channel_id)
        if info is None:
            return True
        return info.update_stall_tracking(file_path, current_size)

    def get_stall_seconds(self, channel_id: str) -> Optional[float]:
        info = self._procs.get(channel_id)
        return info.stall_seconds if info else None

    def get_last_file_size(self, channel_id: str) -> Optional[int]:
        info = self._procs.get(channel_id)
        return info.last_file_size if info else None

    def get_last_file_size_change_at(self, channel_id: str) -> Optional[datetime]:
        info = self._procs.get(channel_id)
        return info.last_file_size_change_at if info else None

    # ── Restart backoff ───────────────────────────────────────────────────

    def is_in_cooldown(self, channel_id: str) -> bool:
        hist = self._restart_history.get(channel_id)
        return hist.is_in_cooldown() if hist else False

    def get_cooldown_remaining(self, channel_id: str) -> float:
        hist = self._restart_history.get(channel_id)
        return hist.cooldown_remaining_seconds() if hist else 0.0

    def get_restart_count_window(self, channel_id: str) -> int:
        settings = get_settings()
        hist = self._restart_history.get(channel_id)
        if hist is None:
            return 0
        return hist.count_in_window(settings.restart_backoff_window_seconds)

    def get_last_restart_time(self, channel_id: str) -> Optional[datetime]:
        hist = self._restart_history.get(channel_id)
        return hist.last_restart_time() if hist else None

    def attempt_auto_restart(self, channel_id: str) -> bool:
        """
        Gate all auto-restart attempts through the backoff policy.

        Returns True if the caller should proceed with the restart.
        Returns False if the channel is in cooldown or just entered cooldown.
        Also sets health to DEGRADED when multiple restarts have occurred.

        Phase 6.2: persists the restart attempt to the DB so the backoff
        counters survive server restarts.
        """
        settings = get_settings()
        hist = self._get_or_create_history(channel_id)

        # Already in cooldown — skip
        if hist.is_in_cooldown():
            remaining = hist.cooldown_remaining_seconds()
            logger.info(
                "[%s] In COOLDOWN (%.0fs remaining) — auto-restart blocked.",
                channel_id, remaining,
            )
            return False

        # Record this attempt in memory
        hist.record_attempt()
        # Persist to DB (best-effort; never blocks or crashes the watchdog)
        self._persist_restart_attempt(channel_id)

        count = hist.count_in_window(settings.restart_backoff_window_seconds)

        # Too many restarts → enter cooldown
        if count > settings.restart_backoff_max_restarts:
            hist.enter_cooldown(settings.restart_cooldown_seconds)
            # Apply COOLDOWN health to the in-memory process entry (if any)
            info = self._procs.get(channel_id)
            if info:
                info.health = HealthStatus.COOLDOWN
            logger.warning(
                "[%s] Too many restarts (%d in window) — entering COOLDOWN for %ds.",
                channel_id, count, settings.restart_cooldown_seconds,
            )
            return False  # don't restart right now

        # Degraded if this isn't the first restart in the window
        if count > 1:
            self.mark_degraded(channel_id)

        return True

    def _persist_restart_attempt(self, channel_id: str) -> None:
        """Insert a restart_history row for *channel_id* (best-effort)."""
        try:
            from ..db.session import get_session_factory
            SessionLocal = get_session_factory()
            with SessionLocal() as db:
                db.add(RestartHistoryRecord(
                    channel_id=channel_id,
                    attempted_at=utc_now(),
                ))
                db.commit()
        except Exception as exc:
            logger.warning("[%s] Could not persist restart history: %s", channel_id, exc)

    def load_restart_history_from_db(self, db: Session) -> None:
        """
        Phase 6.2: called once at startup.

        Loads recent restart_history rows from the DB into in-memory
        _RestartHistory objects so that backoff counters survive server restarts.
        Only rows within the backoff window are loaded.
        """
        from datetime import timedelta
        settings = get_settings()
        window_seconds = settings.restart_backoff_window_seconds
        since = utc_now() - timedelta(seconds=window_seconds)

        try:
            rows = (
                db.query(RestartHistoryRecord)
                .filter(RestartHistoryRecord.attempted_at >= since)
                .order_by(RestartHistoryRecord.attempted_at)
                .all()
            )
            for row in rows:
                hist = self._get_or_create_history(row.channel_id)
                hist._timestamps.append(row.attempted_at)
            if rows:
                logger.info(
                    "[process-manager] Loaded %d restart history record(s) from DB.",
                    len(rows),
                )
        except Exception as exc:
            logger.warning("[process-manager] Could not load restart history: %s", exc)

    # ── Core lifecycle ────────────────────────────────────────────────────

    def _preflight_check(self, channel_id: str, config: ChannelConfig) -> None:
        """
        Validate the channel configuration before launching FFmpeg.

        Raises :exc:`ValueError` with a human-readable message when a
        configuration problem is detected so that the API layer can return a
        clear HTTP 400 response instead of a cryptic 500.

        Checks performed:
        - ``ffmpeg_path`` is set and points to an existing file (skipped if the
          path is a bare name such as ``"ffmpeg"`` without a directory separator,
          which relies on ``$PATH`` lookup).
        - Date-based mode requires ``record_root`` to be set.
        - ``record_root`` (date-based) or ``record_dir`` (legacy) must be
          creatable/reachable.
        - FFmpeg output pattern must be non-empty (i.e. the path builder did
          not silently fall back to an empty string).
        - When ``recording_preview_output.mode == "hls_direct"``, the preview
          output directory must be creatable.
        """
        from .ffmpeg_builder import _output_pattern as _op
        from pathlib import Path as _Path

        paths = config.paths

        # ── ffmpeg binary exists ───────────────────────────────────────────
        ffmpeg = config.ffmpeg_path
        if not ffmpeg:
            raise ValueError(
                f"[{channel_id}] ffmpeg_path is empty. "
                "Set a valid path to the ffmpeg binary in the channel config."
            )
        # Only validate if the value looks like a full path (contains a separator)
        if (_Path(ffmpeg).parent != _Path(".")) and not _Path(ffmpeg).is_file():
            raise ValueError(
                f"[{channel_id}] ffmpeg binary not found at '{ffmpeg}'. "
                "Check the ffmpeg_path setting in the channel config."
            )

        # ── Date-based mode requires record_root ──────────────────────────
        if paths.effective_use_date_folders:
            if not paths.record_root:
                raise ValueError(
                    f"[{channel_id}] Date-based recording mode is active but "
                    "paths.record_root is not set. "
                    "Add \"record_root\" to the channel's paths config, e.g.: "
                    "\"D:\\\\AutoRec\\\\record\\\\rts1\"."
                )
            # Ensure record_root is reachable/creatable
            root = resolve_channel_path(paths.record_root)
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(
                    f"[{channel_id}] Cannot create/access record_root '{root}': {exc}"
                ) from exc
        elif not paths.record_dir:
            raise ValueError(
                f"[{channel_id}] Neither record_root (date-based) nor record_dir "
                "(legacy) is configured. Add one of these to the channel's paths config."
            )

        # ── Output pattern must be non-empty (date-based mode only) ──────────
        # In date-based mode the output pattern is built entirely from record_root;
        # an empty string means record_root was not resolved correctly.
        # Legacy mode (record_dir set) does not use this pattern builder.
        if paths.effective_use_date_folders:
            try:
                pattern = _op(config)
            except Exception as exc:
                raise ValueError(
                    f"[{channel_id}] Failed to build FFmpeg output path pattern: {exc}"
                ) from exc
            if not pattern:
                raise ValueError(
                    f"[{channel_id}] FFmpeg output path pattern is empty. "
                    "Ensure record_root is configured for date-based mode."
                )

        # ── HLS direct preview output dir ─────────────────────────────────
        rpo = config.recording_preview_output
        if rpo is not None and rpo.enabled and getattr(rpo, "mode", "udp") == "hls_direct":
            settings = get_settings()
            preview_dir = settings.preview_dir / channel_id
            try:
                preview_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(
                    f"[{channel_id}] Cannot create HLS preview directory "
                    f"'{preview_dir}': {exc}"
                ) from exc

    def start(self, channel_id: str, config: ChannelConfig, db: Session) -> ProcessInfo:
        """
        Launch FFmpeg for *channel_id*.

        Raises RuntimeError if already recording.
        Raises ValueError if the channel configuration is invalid (preflight check).
        stdout is suppressed; stderr goes to a timestamped log file.
        On Windows, CREATE_NO_WINDOW suppresses the console (equivalent to start /min).

        Phase 6.2: checks minimum free disk space before launching.
        Phase 27: preflight config validation; full traceback logging on errors;
                  date-based mode no longer requires record_dir/chunks_dir.
        """
        if self.is_running(channel_id):
            raise RuntimeError(f"Channel '{channel_id}' is already recording.")

        # Phase 27 — Preflight config validation (raises ValueError on bad config)
        self._preflight_check(channel_id, config)

        # Phase 6.2 — Disk space safety check
        # Use record_root for date-based channels; record_dir for legacy channels.
        settings = get_settings()
        min_free = settings.min_free_disk_bytes
        if min_free > 0:
            paths = config.paths
            if paths.effective_use_date_folders and paths.record_root:
                _check_path = resolve_channel_path(paths.record_root)
            elif paths.record_dir:
                _check_path = resolve_channel_path(paths.record_dir)
            else:
                _check_path = None

            if _check_path is not None:
                check_dir = _check_path if _check_path.exists() else _check_path.parent
                try:
                    free = shutil.disk_usage(str(check_dir)).free
                    if free < min_free:
                        msg = (
                            f"[{channel_id}] Insufficient disk space: "
                            f"{free // (1024 * 1024)} MB free, "
                            f"minimum required {min_free // (1024 * 1024)} MB. "
                            "Recording NOT started."
                        )
                        logger.error(msg)
                        self.mark_degraded(channel_id)
                        raise RuntimeError(msg)
                except OSError as exc:
                    logger.warning("[%s] Could not check disk space: %s", channel_id, exc)

        # Phase 27 — Build FFmpeg command with full traceback on failure
        try:
            cmd = build_ffmpeg_command(config)
        except Exception:
            logger.exception(
                "[%s] Failed to build FFmpeg command. "
                "Check channel config (record_root, ffmpeg_path, filters).",
                channel_id,
            )
            raise

        log_path = self._new_log_path(channel_id)
        self._prune_old_logs(channel_id)

        # Ensure output directory/directories exist before FFmpeg tries to write.
        # Date-based mode: pre-create today's and tomorrow's date folders so
        # FFmpeg's stream_segment muxer (which does not create directories) can
        # write files across midnight without failing.
        # Legacy mode: create the configured record_dir.
        paths = config.paths
        if paths.effective_use_date_folders and paths.record_root:
            from .ffmpeg_builder import ensure_date_folders
            ensure_date_folders(config)
        elif paths.record_dir:
            resolve_channel_path(paths.record_dir).mkdir(parents=True, exist_ok=True)

        # Write command header to log (text mode)
        now_iso = utc_now().isoformat()
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"[{now_iso}] COMMAND: {format_command_for_log(cmd)}\n")
            lf.write(f"[{now_iso}] STARTING\n")

        logger.info("[%s] Starting FFmpeg: %s", channel_id, format_command_for_log(cmd))

        # Open log in binary-append mode for subprocess stderr.
        # The file handle is always closed in the finally block; the subprocess
        # retains its own copy of the descriptor and continues writing to it.
        log_fh = open(log_path, "ab")  # noqa: WPS515
        try:
            extra: dict = {}
            if sys.platform == "win32":
                extra["creationflags"] = subprocess.CREATE_NO_WINDOW

            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=log_fh,
                    **extra,
                )
            except Exception:
                logger.exception(
                    "[%s] subprocess.Popen() failed. "
                    "Command: %s",
                    channel_id,
                    format_command_for_log(cmd),
                )
                raise
        finally:
            # Always close the parent's copy — subprocess keeps its own fd.
            log_fh.close()

        started_at = utc_now()
        info = ProcessInfo(
            channel_id=channel_id,
            pid=process.pid,
            process=process,
            log_path=log_path,
            started_at=started_at,
        )
        self._procs[channel_id] = info

        # Persist to DB
        record = ProcessRecord(
            channel_id=channel_id,
            pid=process.pid,
            status=ProcessStatus.RUNNING.value,
            started_at=started_at,
            log_path=str(log_path),
        )
        db.add(record)
        db.commit()

        logger.info("[%s] FFmpeg started — PID %d, log: %s", channel_id, process.pid, log_path)

        # ── Phase 12 — NVENC fallback ─────────────────────────────────────────
        # If the preview output is configured to use an NVENC encoder and
        # fallback_to_cpu=True, wait briefly to detect an immediate crash caused
        # by NVENC being unavailable.  On failure, retry once with libx264.
        # This is the ONLY place a retry occurs; we never loop.
        rpo = config.recording_preview_output
        if (
            rpo is not None
            and rpo.enabled
            and rpo.fallback_to_cpu
            and rpo.video_codec != "libx264"
        ):
            time.sleep(_NVENC_CRASH_WAIT)
            exit_code = process.poll()
            if exit_code is not None:  # exited before the wait expired
                if _is_nvenc_failure(log_path):
                    logger.warning(
                        "[%s] NVENC preview failed, retrying recording with "
                        "CPU preview encoder.",
                        channel_id,
                    )
                    # Mark the failed first attempt as stopped in the DB.
                    failed_record = (
                        db.query(ProcessRecord)
                        .filter(
                            ProcessRecord.channel_id == channel_id,
                            ProcessRecord.pid == process.pid,
                            ProcessRecord.status == ProcessStatus.RUNNING.value,
                        )
                        .order_by(ProcessRecord.id.desc())
                        .first()
                    )
                    if failed_record:
                        failed_record.status = ProcessStatus.STOPPED.value
                        failed_record.stopped_at = utc_now()
                        failed_record.exit_code = exit_code
                        db.commit()

                    # Remove the failed entry from in-memory state.
                    del self._procs[channel_id]

                    # Build a new config with libx264 for the preview output.
                    # Main recording settings (config.encoding.*) are unchanged.
                    cpu_rpo = rpo.model_copy(
                        update={"video_codec": "libx264", "tune": None, "preset": "veryfast"}
                    )
                    cpu_config = config.model_copy(
                        update={"recording_preview_output": cpu_rpo}
                    )

                    cmd = build_ffmpeg_command(cpu_config)
                    log_path = self._new_log_path(channel_id)

                    now_iso = utc_now().isoformat()
                    with open(log_path, "w", encoding="utf-8") as lf:
                        lf.write(
                            f"[{now_iso}] NVENC FALLBACK: retrying with libx264 "
                            "preview encoder\n"
                        )
                        lf.write(f"[{now_iso}] COMMAND: {format_command_for_log(cmd)}\n")
                        lf.write(f"[{now_iso}] STARTING\n")

                    logger.info(
                        "[%s] Starting FFmpeg (CPU fallback): %s",
                        channel_id,
                        format_command_for_log(cmd),
                    )

                    log_fh = open(log_path, "ab")
                    try:
                        extra: dict = {}
                        if sys.platform == "win32":
                            extra["creationflags"] = subprocess.CREATE_NO_WINDOW

                        process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=log_fh,
                            **extra,
                        )
                    finally:
                        log_fh.close()

                    started_at = utc_now()
                    info = ProcessInfo(
                        channel_id=channel_id,
                        pid=process.pid,
                        process=process,
                        log_path=log_path,
                        started_at=started_at,
                    )
                    self._procs[channel_id] = info

                    record = ProcessRecord(
                        channel_id=channel_id,
                        pid=process.pid,
                        status=ProcessStatus.RUNNING.value,
                        started_at=started_at,
                        log_path=str(log_path),
                    )
                    db.add(record)
                    db.commit()

                    logger.info(
                        "[%s] FFmpeg (CPU fallback) started — PID %d, log: %s",
                        channel_id,
                        process.pid,
                        log_path,
                    )

        return info

    def stop(
        self, channel_id: str, db: Session, timeout: Optional[int] = None
    ) -> bool:
        """
        Gracefully stop a running FFmpeg process.

        Sends SIGTERM (or TerminateProcess on Windows), waits *timeout* seconds,
        then SIGKILL.  Returns True if a process was stopped, False if not running.
        Works for both owned (Popen) and adopted (PID-only) processes.
        """
        self._reap_if_dead(channel_id)
        info = self._procs.get(channel_id)
        if info is None:
            logger.info("[%s] Stop requested but not running.", channel_id)
            return False

        if timeout is None:
            timeout = get_settings().stop_timeout_seconds

        pid = info.pid
        logger.info("[%s] Stopping PID %d (timeout=%ds).", channel_id, pid, timeout)

        try:
            if info.process is not None:
                # Owned Popen process
                if sys.platform == "win32":
                    info.process.terminate()
                else:
                    info.process.send_signal(signal.SIGTERM)
                try:
                    info.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "[%s] SIGTERM timeout — sending SIGKILL to PID %d.", channel_id, pid
                    )
                    info.process.kill()
                    info.process.wait(timeout=5)
            else:
                # Adopted orphan — use os.kill directly
                try:
                    if sys.platform == "win32":
                        import ctypes
                        PROCESS_TERMINATE = 0x0001
                        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                        if handle:
                            ctypes.windll.kernel32.TerminateProcess(handle, 0)
                            ctypes.windll.kernel32.CloseHandle(handle)
                    else:
                        os.kill(pid, signal.SIGTERM)
                    _wait_for_pid_death(pid, timeout)
                    if _pid_exists(pid):
                        if sys.platform != "win32":
                            os.kill(pid, signal.SIGKILL)
                        _wait_for_pid_death(pid, 5)
                except OSError:
                    pass
        except Exception as exc:
            logger.error("[%s] Error while stopping process: %s", channel_id, exc)

        exit_code = info.exit_code()
        stopped_at = utc_now()
        del self._procs[channel_id]

        # Update the most recent running record in DB
        record = (
            db.query(ProcessRecord)
            .filter(
                ProcessRecord.channel_id == channel_id,
                ProcessRecord.pid == pid,
                ProcessRecord.status == ProcessStatus.RUNNING.value,
            )
            .order_by(ProcessRecord.id.desc())
            .first()
        )
        if record:
            record.status = ProcessStatus.STOPPED.value
            record.stopped_at = stopped_at
            record.exit_code = exit_code
            db.commit()

        logger.info("[%s] Stopped (PID %d, exit_code=%s).", channel_id, pid, exit_code)
        return True

    def restart(self, channel_id: str, config: ChannelConfig, db: Session) -> ProcessInfo:
        """
        Stop (if running) then start, with a short pre-delay buffer.

        The pre-delay (restart_pre_delay_seconds) gives the OS time to fully
        clean up the previous process before a new one is launched.
        """
        self.stop(channel_id, db)
        delay = get_settings().restart_pre_delay_seconds
        if delay > 0:
            time.sleep(delay)
        return self.start(channel_id, config, db)

    def get_log_tail(self, channel_id: str, lines: int = 100) -> list[str]:
        """Return the last *lines* lines from the channel's active (or latest) log."""
        log_path = self.get_log_path(channel_id)
        if log_path is None:
            return []
        return _tail_file(log_path, lines)

    def reconcile_on_startup(self, db: Session) -> None:
        """
        Called once at application startup.

        For each ProcessRecord with status=RUNNING:
        - If the PID is still alive → adopt it (register in _procs so the watchdog
          picks it up and monitors it going forward).
        - If the PID is dead (or unknown) → mark the record as STOPPED.
        """
        stale_statuses = {ProcessStatus.RUNNING.value, ProcessStatus.STARTING.value}
        stale_records = (
            db.query(ProcessRecord)
            .filter(ProcessRecord.status.in_(stale_statuses))
            .order_by(ProcessRecord.id.desc())  # newest first so we adopt the latest
            .all()
        )
        if not stale_records:
            return

        seen_channels: set[str] = set()
        adopted = 0
        marked_stopped = 0

        for rec in stale_records:
            channel_id = rec.channel_id

            # Only process the most-recent record per channel
            if channel_id in seen_channels:
                rec.status = ProcessStatus.STOPPED.value
                rec.stopped_at = utc_now()
                marked_stopped += 1
                continue
            seen_channels.add(channel_id)

            was_alive = bool(rec.pid and _pid_exists(rec.pid))

            if was_alive and channel_id not in self._procs:
                # Adopt the orphaned process
                log_path = Path(rec.log_path) if rec.log_path else self._new_log_path(channel_id)
                started_at = rec.started_at or utc_now()
                info = ProcessInfo(
                    channel_id=channel_id,
                    pid=rec.pid,
                    process=None,  # no Popen handle for adopted processes
                    log_path=log_path,
                    started_at=started_at,
                )
                self._procs[channel_id] = info
                rec.adopted = True
                adopted += 1
                logger.info("[%s] Adopted orphaned PID %d.", channel_id, rec.pid)
            else:
                rec.status = ProcessStatus.STOPPED.value
                rec.stopped_at = utc_now()
                marked_stopped += 1
                if rec.pid:
                    logger.warning(
                        "[%s] Stale PID %d (alive=%s) — marked stopped.",
                        channel_id, rec.pid, was_alive,
                    )

        db.commit()
        if adopted or marked_stopped:
            logger.info(
                "Startup reconcile: %d adopted, %d marked stopped.", adopted, marked_stopped
            )


# ─── Module-level singleton ────────────────────────────────────────────────────

_manager: Optional[ProcessManager] = None


def get_process_manager() -> ProcessManager:
    """Return the application-wide ProcessManager singleton."""
    global _manager
    if _manager is None:
        _manager = ProcessManager()
    return _manager

