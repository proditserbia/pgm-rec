"""
Preview Process Manager — Phase 2.

Manages a lightweight FFmpeg preview process per channel, completely
isolated from the recording pipeline.

Architecture
────────────
Each channel that has preview enabled or is started via the API gets:

1. A separate FFmpeg subprocess with:
   - Same dshow/v4l2 input as recording (device multi-access assumed, or
     documented as a limitation when the hardware doesn't support it)
   - Video scaled down + fps reduced
   - Audio disabled
   - Output: raw MJPEG frames on stdout (``-f mjpeg pipe:1``)

2. A background daemon thread (``_FrameReader``) that:
   - Continuously reads MJPEG frames from the process stdout
   - Parses frame boundaries (0xFF 0xD8 … 0xFF 0xD9)
   - Stores the latest complete JPEG in memory

3. A StreamingResponse generator (in the API layer) that polls the latest
   frame at the requested fps.

Isolation guarantees
────────────────────
- PreviewManager is a completely separate singleton from ProcessManager.
- A preview crash NEVER touches the recording state.
- The recording watchdog and restart logic are not involved.
- Preview watchdog only marks preview DOWN; it does NOT auto-restart.

Cross-platform
────────────────────
- Uses subprocess.Popen (shell=False, stdout=PIPE)
- Threading for background reads (safe with asyncio via run_in_executor or
  asyncio.to_thread for the watchdog)
- Works on both Windows and Linux
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config.settings import get_settings
from ..models.schemas import ChannelConfig, PreviewHealth
from .ffmpeg_builder import build_preview_command, format_command_for_log

logger = logging.getLogger(__name__)

# JPEG SOI (Start Of Image) and EOI (End Of Image) markers
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
# Maximum frame size we accept before discarding the buffer (10 MB)
_MAX_FRAME_SIZE = 10 * 1024 * 1024


# ─── Background frame reader ──────────────────────────────────────────────────

class _FrameReader(threading.Thread):
    """
    Daemon thread that reads raw MJPEG bytes from *process.stdout*, finds
    complete JPEG frames (SOI … EOI), and stores the latest frame.

    Runs for the lifetime of the preview process.  When the process exits,
    stdout.read() returns b"" and the thread exits cleanly.
    """

    def __init__(self, process: subprocess.Popen, channel_id: str) -> None:
        super().__init__(daemon=True, name=f"pgmrec-preview-{channel_id}")
        self._process = process
        self._channel_id = channel_id
        self._lock = threading.Lock()
        self._latest_frame: Optional[bytes] = None
        self._frame_count: int = 0

    def run(self) -> None:
        buf = bytearray()
        logger.debug("[preview][%s] Frame reader thread started.", self._channel_id)
        try:
            while self._process.poll() is None:
                chunk = self._process.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                self._parse_frames(buf)
        except OSError as exc:
            logger.debug(
                "[preview][%s] Frame reader I/O error: %s", self._channel_id, exc
            )
        finally:
            logger.debug(
                "[preview][%s] Frame reader thread exiting (frames read: %d).",
                self._channel_id,
                self._frame_count,
            )

    def _parse_frames(self, buf: bytearray) -> None:
        """Extract and store all complete JPEG frames found in *buf* in-place."""
        while True:
            # Find start of next JPEG
            start = buf.find(_JPEG_SOI)
            if start == -1:
                buf.clear()
                return

            # Discard bytes before the SOI
            if start > 0:
                del buf[:start]

            # Find end of current JPEG
            end = buf.find(_JPEG_EOI, 2)
            if end == -1:
                # Incomplete frame — keep accumulating
                if len(buf) > _MAX_FRAME_SIZE:
                    # Something went wrong; dump the buffer to avoid memory growth
                    logger.warning(
                        "[preview][%s] Frame buffer exceeded %d bytes — resetting.",
                        self._channel_id,
                        _MAX_FRAME_SIZE,
                    )
                    buf.clear()
                return

            frame = bytes(buf[: end + 2])
            del buf[: end + 2]

            with self._lock:
                self._latest_frame = frame
                self._frame_count += 1

    @property
    def latest_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_frame

    @property
    def frame_count(self) -> int:
        with self._lock:
            return self._frame_count


# ─── Per-channel preview state ────────────────────────────────────────────────

@dataclass
class PreviewInfo:
    """In-memory state for one active channel preview process."""

    channel_id: str
    pid: int
    log_path: Path
    started_at: datetime
    process: subprocess.Popen
    reader: _FrameReader
    health: PreviewHealth = PreviewHealth.UNKNOWN

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def mark_healthy(self) -> None:
        self.health = PreviewHealth.HEALTHY

    def mark_down(self) -> None:
        self.health = PreviewHealth.DOWN

    def get_latest_frame(self) -> Optional[bytes]:
        return self.reader.latest_frame


# ─── Preview manager ──────────────────────────────────────────────────────────

class PreviewManager:
    """
    Singleton that owns all FFmpeg preview subprocesses.

    Completely independent of ProcessManager — no shared state.
    """

    def __init__(self) -> None:
        self._previews: dict[str, PreviewInfo] = {}

    # ── Internal helpers ──────────────────────────────────────────────────

    def _new_log_path(self, channel_id: str) -> Path:
        """Return a timestamped log file path for the preview process."""
        settings = get_settings()
        log_dir = settings.logs_dir / "channels" / channel_id
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return log_dir / f"preview-{ts}.log"

    def _reap_if_dead(self, channel_id: str) -> None:
        """Remove a channel entry if its preview process has already exited."""
        info = self._previews.get(channel_id)
        if info and not info.is_alive():
            logger.info(
                "[preview][%s] Process PID %d exited (returncode=%s).",
                channel_id,
                info.pid,
                info.process.returncode,
            )
            del self._previews[channel_id]

    def _stream_url(self, channel_id: str) -> str:
        """Return the FastAPI proxy stream URL for this channel."""
        return f"/api/v1/channels/{channel_id}/preview/stream"

    # ── Public interface ──────────────────────────────────────────────────

    def is_running(self, channel_id: str) -> bool:
        self._reap_if_dead(channel_id)
        return channel_id in self._previews

    def get_pid(self, channel_id: str) -> Optional[int]:
        self._reap_if_dead(channel_id)
        info = self._previews.get(channel_id)
        return info.pid if info else None

    def get_health(self, channel_id: str) -> PreviewHealth:
        self._reap_if_dead(channel_id)
        info = self._previews.get(channel_id)
        if info is None:
            return PreviewHealth.UNKNOWN
        return info.health

    def get_latest_frame(self, channel_id: str) -> Optional[bytes]:
        """Return the latest JPEG frame for *channel_id*, or None if unavailable."""
        info = self._previews.get(channel_id)
        if info is None or not info.is_alive():
            return None
        return info.get_latest_frame()

    def start_preview(self, channel_id: str, config: ChannelConfig) -> PreviewInfo:
        """
        Launch a preview FFmpeg process for *channel_id*.

        Raises RuntimeError if a preview is already running.
        stdout is piped (read by background thread); stderr goes to a log file.
        """
        if self.is_running(channel_id):
            raise RuntimeError(f"Preview for channel '{channel_id}' is already running.")

        cmd = build_preview_command(config)
        log_path = self._new_log_path(channel_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"[{now_iso}] PREVIEW COMMAND: {format_command_for_log(cmd)}\n")
            lf.write(f"[{now_iso}] STARTING\n")

        logger.info(
            "[preview][%s] Starting: %s", channel_id, format_command_for_log(cmd)
        )

        log_fh = open(log_path, "ab")
        try:
            extra: dict = {}
            if sys.platform == "win32":
                extra["creationflags"] = subprocess.CREATE_NO_WINDOW

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=log_fh,
                **extra,
            )
        finally:
            log_fh.close()

        reader = _FrameReader(process, channel_id)
        reader.start()

        started_at = datetime.now(timezone.utc)
        info = PreviewInfo(
            channel_id=channel_id,
            pid=process.pid,
            log_path=log_path,
            started_at=started_at,
            process=process,
            reader=reader,
            health=PreviewHealth.HEALTHY,
        )
        self._previews[channel_id] = info

        logger.info(
            "[preview][%s] Started — PID %d, log: %s", channel_id, process.pid, log_path
        )
        return info

    def stop_preview(self, channel_id: str) -> bool:
        """
        Stop the preview process for *channel_id*.

        Returns True if a process was stopped, False if not running.
        Uses SIGTERM → timeout → SIGKILL (same as recording stop).
        """
        self._reap_if_dead(channel_id)
        info = self._previews.get(channel_id)
        if info is None:
            logger.info("[preview][%s] Stop requested but not running.", channel_id)
            return False

        pid = info.pid
        timeout = get_settings().stop_timeout_seconds
        logger.info("[preview][%s] Stopping PID %d (timeout=%ds).", channel_id, pid, timeout)

        try:
            if sys.platform == "win32":
                info.process.terminate()
            else:
                import signal
                info.process.send_signal(signal.SIGTERM)
            try:
                info.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "[preview][%s] SIGTERM timeout — sending SIGKILL to PID %d.",
                    channel_id, pid,
                )
                info.process.kill()
                info.process.wait(timeout=5)
        except Exception as exc:
            logger.error("[preview][%s] Error stopping process: %s", channel_id, exc)

        del self._previews[channel_id]
        logger.info("[preview][%s] Stopped (PID %d).", channel_id, pid)
        return True

    def preview_status(self, channel_id: str) -> dict:
        """
        Return a status dict for *channel_id*.

        Keys: running, pid, started_at, stream_url, health
        """
        self._reap_if_dead(channel_id)
        info = self._previews.get(channel_id)
        if info is None:
            return {
                "running": False,
                "pid": None,
                "started_at": None,
                "stream_url": None,
                "health": PreviewHealth.UNKNOWN,
            }
        return {
            "running": True,
            "pid": info.pid,
            "started_at": info.started_at,
            "stream_url": self._stream_url(channel_id),
            "health": info.health,
        }

    # ── Watchdog (light version) ───────────────────────────────────────────

    def check_all(self) -> None:
        """
        Called by the preview watchdog loop.

        Only checks liveness — marks DOWN if the process has exited.
        Does NOT auto-restart (keeps recording pipeline unaffected).
        """
        for channel_id in list(self._previews):
            info = self._previews.get(channel_id)
            if info is None:
                continue
            if not info.is_alive():
                logger.warning(
                    "[preview][%s] Process PID %d exited — marking DOWN.",
                    channel_id, info.pid,
                )
                info.mark_down()
                # Remove from active dict so next status call returns "not running"
                del self._previews[channel_id]
            else:
                info.mark_healthy()


# ─── Module-level singleton ────────────────────────────────────────────────────

_preview_manager: Optional[PreviewManager] = None


def get_preview_manager() -> PreviewManager:
    """Return the application-wide PreviewManager singleton."""
    global _preview_manager
    if _preview_manager is None:
        _preview_manager = PreviewManager()
    return _preview_manager


# ─── Preview watchdog loop (independent asyncio Task) ─────────────────────────

async def run_preview_watchdog_loop() -> None:
    """
    Independent asyncio Task — runs its own interval loop.

    Checks all running previews at the same interval as the recording watchdog.
    Uses asyncio.to_thread to avoid blocking the event loop.
    """
    settings = get_settings()
    interval = settings.watchdog_interval_seconds
    logger.info("Preview watchdog: started (interval=%ds).", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_run_preview_watchdog_sync)
        except Exception:
            logger.exception("Preview watchdog: unexpected error.")


def _run_preview_watchdog_sync() -> None:
    """Synchronous body of the preview watchdog — safe to run in a thread."""
    get_preview_manager().check_all()
