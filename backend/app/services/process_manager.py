"""
Process manager for PGMRec FFmpeg recording processes.

Manages the full lifecycle of one FFmpeg process per channel:
  - start    — Popen with PID tracking, stderr → log file
  - stop     — SIGTERM → timeout → SIGKILL
  - restart  — stop + start
  - status   — live poll of the OS process
  - log tail — read last N lines from the active log file

Design rules:
  - PID-based tracking ONLY (no window titles)
  - Never block the API — subprocess is launched then detached
  - Always log FFmpeg stderr
  - Safe subprocess execution (shell=False)
  - Multi-channel from day one (keyed by channel_id str)
  - Adopted processes (orphaned from a previous server run) are tracked by PID only
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..config.settings import get_settings
from ..db.models import ProcessRecord
from ..models.schemas import ChannelConfig, HealthStatus, ProcessStatus
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

    All liveness checks use is_alive(), which handles both cases.
    """

    channel_id: str
    pid: int
    log_path: Path
    started_at: datetime
    process: Optional[subprocess.Popen] = field(default=None)
    last_seen_alive: datetime = field(init=False)
    health: HealthStatus = field(default=HealthStatus.UNKNOWN)

    def __post_init__(self) -> None:
        self.last_seen_alive = datetime.now(timezone.utc)

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
        self.last_seen_alive = datetime.now(timezone.utc)
        self.health = HealthStatus.HEALTHY

    def mark_unhealthy(self) -> None:
        self.health = HealthStatus.UNHEALTHY


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
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
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

    def start(self, channel_id: str, config: ChannelConfig, db: Session) -> ProcessInfo:
        """
        Launch FFmpeg for *channel_id*.

        Raises RuntimeError if already recording.
        stdout is suppressed; stderr goes to a timestamped log file.
        On Windows, CREATE_NO_WINDOW suppresses the console (equivalent to start /min).
        """
        if self.is_running(channel_id):
            raise RuntimeError(f"Channel '{channel_id}' is already recording.")

        cmd = build_ffmpeg_command(config)
        log_path = self._new_log_path(channel_id)
        self._prune_old_logs(channel_id)

        # Ensure output directory exists before FFmpeg tries to write there
        Path(config.paths.record_dir).mkdir(parents=True, exist_ok=True)

        # Write command header to log (text mode)
        now_iso = datetime.now(timezone.utc).isoformat()
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

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_fh,
                **extra,
            )
        finally:
            # Always close the parent's copy — subprocess keeps its own fd.
            log_fh.close()

        started_at = datetime.now(timezone.utc)
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
        stopped_at = datetime.now(timezone.utc)
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
        """Stop (if running) then start."""
        self.stop(channel_id, db)
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

        We deliberately do NOT restart dead channels automatically here; the
        watchdog will handle that once it starts running.
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
                rec.stopped_at = datetime.now(timezone.utc)
                marked_stopped += 1
                continue
            seen_channels.add(channel_id)

            was_alive = bool(rec.pid and _pid_exists(rec.pid))

            if was_alive and channel_id not in self._procs:
                # Adopt the orphaned process
                log_path = Path(rec.log_path) if rec.log_path else self._new_log_path(channel_id)
                started_at = rec.started_at or datetime.now(timezone.utc)
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
                rec.stopped_at = datetime.now(timezone.utc)
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
