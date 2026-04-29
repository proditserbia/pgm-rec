"""
Process manager for PGMRec FFmpeg recording processes.

Manages the full lifecycle of one FFmpeg process per channel:
  - start  — Popen with PID tracking, stderr → log file
  - stop   — SIGTERM → timeout → SIGKILL
  - restart — stop + start
  - status  — live poll of the OS process
  - log tail — read last N lines from the active log file

Design rules (from problem statement):
  - PID-based tracking ONLY (no window titles)
  - Never block the API — subprocess is launched then detached
  - Always log FFmpeg stderr
  - Safe subprocess execution (shell=False)
  - Multi-channel from day one (keyed by channel_id str)
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..config.settings import get_settings
from ..db.models import ProcessRecord
from ..models.schemas import ChannelConfig, ProcessStatus
from .ffmpeg_builder import build_ffmpeg_command, format_command_for_log

logger = logging.getLogger(__name__)


# ─── In-memory process state ──────────────────────────────────────────────────

@dataclass
class ProcessInfo:
    """Runtime state for one active channel recording process."""

    channel_id: str
    process: subprocess.Popen
    log_path: Path
    started_at: datetime
    pid: int = field(init=False)

    def __post_init__(self) -> None:
        self.pid = self.process.pid

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def exit_code(self) -> Optional[int]:
        return self.process.returncode


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


# ─── Process manager ──────────────────────────────────────────────────────────

class ProcessManager:
    """
    Singleton that owns all FFmpeg subprocesses for every channel.

    Thread safety: FastAPI runs in an async event loop with a single thread by
    default (uvicorn), so simple dict operations are safe.  If you switch to a
    multi-worker deployment, move state to the DB or Redis.
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
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        return log_dir / f"ffmpeg-{ts}.log"

    # ── Public interface ──────────────────────────────────────────────────

    def is_running(self, channel_id: str) -> bool:
        self._reap_if_dead(channel_id)
        return channel_id in self._procs

    def get_status(self, channel_id: str) -> ProcessStatus:
        return ProcessStatus.RUNNING if self.is_running(channel_id) else ProcessStatus.STOPPED

    def get_pid(self, channel_id: str) -> Optional[int]:
        self._reap_if_dead(channel_id)
        info = self._procs.get(channel_id)
        return info.pid if info else None

    def get_started_at(self, channel_id: str) -> Optional[datetime]:
        self._reap_if_dead(channel_id)
        info = self._procs.get(channel_id)
        return info.started_at if info else None

    def get_log_path(self, channel_id: str) -> Optional[Path]:
        info = self._procs.get(channel_id)
        if info:
            return info.log_path
        return self._latest_log_for(channel_id)

    def start(self, channel_id: str, config: ChannelConfig, db: Session) -> ProcessInfo:
        """
        Launch FFmpeg for *channel_id*.

        Raises RuntimeError if already recording.
        The process stdout is suppressed; stderr goes to a timestamped log file.
        On Windows, CREATE_NO_WINDOW suppresses the console (equivalent to start /min).
        """
        if self.is_running(channel_id):
            raise RuntimeError(f"Channel '{channel_id}' is already recording.")

        cmd = build_ffmpeg_command(config)
        log_path = self._new_log_path(channel_id)

        # Ensure output directory exists before FFmpeg tries to write there
        Path(config.paths.record_dir).mkdir(parents=True, exist_ok=True)

        # Write command header to log (text mode)
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"[{datetime.now().isoformat()}] COMMAND: {format_command_for_log(cmd)}\n")
            lf.write(f"[{datetime.now().isoformat()}] STARTING\n")

        logger.info("[%s] Starting FFmpeg: %s", channel_id, format_command_for_log(cmd))

        # Open log in binary-append mode for subprocess stderr
        log_fh = open(log_path, "ab")  # noqa: WPS515 (intentional open outside with)
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
        except Exception:
            log_fh.close()
            raise
        finally:
            # Parent no longer needs the handle; subprocess retains its own copy
            log_fh.close()

        started_at = datetime.utcnow()
        info = ProcessInfo(
            channel_id=channel_id,
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
        except Exception as exc:
            logger.error("[%s] Error while stopping process: %s", channel_id, exc)

        exit_code = info.process.returncode
        stopped_at = datetime.utcnow()
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

        Finds any DB records left in RUNNING state (from a previous server crash or
        restart) and marks them STOPPED.  We deliberately do NOT reattach to orphaned
        PIDs — the operator should restart recording explicitly.
        """
        stale_statuses = {ProcessStatus.RUNNING.value, ProcessStatus.STARTING.value}
        stale_records = (
            db.query(ProcessRecord)
            .filter(ProcessRecord.status.in_(stale_statuses))
            .all()
        )
        if not stale_records:
            return

        for rec in stale_records:
            was_alive = rec.pid and _pid_exists(rec.pid)
            logger.warning(
                "[%s] Reconciling stale PID %s (was_alive=%s).",
                rec.channel_id,
                rec.pid,
                was_alive,
            )
            rec.status = ProcessStatus.STOPPED.value
            rec.stopped_at = datetime.utcnow()
        db.commit()
        logger.info("Reconciled %d stale process record(s).", len(stale_records))


# ─── Module-level singleton ────────────────────────────────────────────────────

_manager: Optional[ProcessManager] = None


def get_process_manager() -> ProcessManager:
    """Return the application-wide ProcessManager singleton."""
    global _manager
    if _manager is None:
        _manager = ProcessManager()
    return _manager
