"""
HLS Preview Process Manager — Phase 5 / Phase 9.

Manages a lightweight FFmpeg HLS-output preview process per channel.
Completely isolated from the recording pipeline.

Architecture
────────────
Each channel that has preview started via the API gets:

1. A separate FFmpeg subprocess that:
   - Reads from the same capture device as recording
   - Scales video down + reduces fps
   - Disables audio
   - Writes HLS output: index.m3u8 + seg*.ts segments under
     data/preview/{channel_id}/

2. The API layer serves index.m3u8 and .ts segments via FileResponse
   endpoints that are protected with JWT auth (any role can view).

3. A light watchdog that marks the preview DOWN if the process exits —
   it NEVER auto-restarts and NEVER touches the recording pipeline.

Isolation guarantees
────────────────────
- HlsPreviewManager is a completely separate singleton from ProcessManager.
- A preview crash NEVER touches the recording state.
- The recording watchdog and restart logic are not involved.
- Preview watchdog only marks DOWN; it does NOT auto-restart.

Phase 9 additions
────────────────────
- playlist_ready: True only when index.m3u8 exists with at least one segment.
- startup_status: "stopped" | "starting" | "running" | "failed"
- startup timeout: if index.m3u8 never appears within
  preview_startup_timeout_seconds, the preview is marked "failed".
- failed_reason: last failure message stored so the UI can display it.
- get_log_tail(): expose preview FFmpeg stderr for in-browser admin view.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config.settings import get_settings
from ..models.schemas import ChannelConfig, PreviewHealth
from .ffmpeg_builder import build_hls_preview_command, format_command_for_log

logger = logging.getLogger(__name__)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _tail_file(path: Path, lines: int) -> list[str]:
    """Return the last *lines* lines of *path* without loading the whole file."""
    if not path or not path.exists():
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
        logger.warning("Cannot read preview log %s: %s", path, exc)
        return []


def _playlist_has_segment(playlist: Path) -> bool:
    """
    Return True if *playlist* exists and contains at least one ``#EXTINF`` tag,
    which indicates at least one media segment has been written.
    """
    if not playlist.exists():
        return False
    try:
        content = playlist.read_text(encoding="utf-8", errors="replace")
        return "#EXTINF" in content
    except OSError:
        return False


# ─── Per-channel HLS preview state ────────────────────────────────────────────

@dataclass
class HlsPreviewInfo:
    """In-memory state for one active channel HLS preview process."""

    channel_id: str
    pid: int
    log_path: Path
    output_dir: Path
    started_at: datetime
    process: subprocess.Popen
    health: PreviewHealth = field(default=PreviewHealth.HEALTHY)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def mark_healthy(self) -> None:
        self.health = PreviewHealth.HEALTHY

    def mark_down(self) -> None:
        self.health = PreviewHealth.DOWN


# ─── Per-channel failure record ────────────────────────────────────────────────

@dataclass
class HlsPreviewFailure:
    """Retained failure information after a preview process exits unexpectedly."""
    reason: str
    log_path: Optional[Path]
    failed_at: datetime


# ─── HLS Preview manager ──────────────────────────────────────────────────────

class HlsPreviewManager:
    """
    Singleton that owns all FFmpeg HLS preview subprocesses.

    Completely independent of ProcessManager — no shared state.
    """

    def __init__(self) -> None:
        self._previews: dict[str, HlsPreviewInfo] = {}
        # Phase 9: retain last failure info per channel so the UI can display it.
        self._failures: dict[str, HlsPreviewFailure] = {}

    # ── Internal helpers ──────────────────────────────────────────────────

    def _new_log_path(self, channel_id: str) -> Path:
        """Return a timestamped log file path for the HLS preview process."""
        settings = get_settings()
        log_dir = settings.logs_dir / "channels" / channel_id
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return log_dir / f"hls-preview-{ts}.log"

    def _output_dir(self, channel_id: str) -> Path:
        """Return the HLS output directory for *channel_id*."""
        return get_settings().preview_dir / channel_id

    def _playlist_api_url(self, channel_id: str) -> str:
        """Return the API endpoint URL for the HLS playlist."""
        return f"/api/v1/channels/{channel_id}/preview/playlist.m3u8"

    def _reap_if_dead(self, channel_id: str) -> None:
        """
        If the preview process has exited, record a failure (if no playlist was
        ever produced) and remove the entry from `_previews`.
        """
        info = self._previews.get(channel_id)
        if info and not info.is_alive():
            playlist = info.output_dir / "index.m3u8"
            # poll() returns the exit code (same as returncode after exit)
            rc = info.process.poll()
            if not _playlist_has_segment(playlist):
                # Process died before producing a usable playlist — record failure.
                tail = _tail_file(info.log_path, 20)
                reason = (
                    f"FFmpeg exited (code={rc}) before playlist was ready."
                )
                if tail:
                    reason += " Last stderr: " + " | ".join(tail[-3:])
                self._failures[channel_id] = HlsPreviewFailure(
                    reason=reason,
                    log_path=info.log_path,
                    failed_at=datetime.now(timezone.utc),
                )
                logger.warning(
                    "[hls-preview][%s] Process PID %d exited without playlist (code=%s).",
                    channel_id, info.pid, rc,
                )
            else:
                logger.info(
                    "[hls-preview][%s] Process PID %d exited (returncode=%s).",
                    channel_id, info.pid, rc,
                )
            del self._previews[channel_id]

    def _check_startup_timeout(self, channel_id: str) -> None:
        """
        Mark a preview as failed if it has been running longer than
        preview_startup_timeout_seconds without producing a usable playlist.
        """
        info = self._previews.get(channel_id)
        if info is None:
            return
        timeout = get_settings().preview_startup_timeout_seconds
        elapsed = (datetime.now(timezone.utc) - info.started_at).total_seconds()
        playlist = info.output_dir / "index.m3u8"
        if elapsed > timeout and not _playlist_has_segment(playlist):
            tail = _tail_file(info.log_path, 20)
            reason = (
                f"Preview timed out after {timeout}s — no playlist produced. "
                "Check: (1) verify exact device name with "
                "'ffmpeg -list_devices true -f dshow -i dummy', "
                "(2) confirm signal is present and SDI standard matches config, "
                "(3) if recording already owns the device, set "
                "preview.input_mode='disabled' in channel config."
            )
            if tail:
                reason += " Last stderr: " + " | ".join(tail[-3:])
            self._failures[channel_id] = HlsPreviewFailure(
                reason=reason,
                log_path=info.log_path,
                failed_at=datetime.now(timezone.utc),
            )
            logger.warning(
                "[hls-preview][%s] Startup timeout — stopping preview PID %d.",
                channel_id, info.pid,
            )
            # Kill the process and remove it
            try:
                info.process.kill()
                info.process.wait(timeout=5)
            except Exception:
                pass
            del self._previews[channel_id]

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
        return info.health if info else PreviewHealth.UNKNOWN

    def get_log_tail(self, channel_id: str, lines: int = 100) -> list[str]:
        """
        Return the last *lines* lines of the current (or most recent failed)
        preview FFmpeg stderr log for *channel_id*.
        """
        info = self._previews.get(channel_id)
        if info:
            return _tail_file(info.log_path, lines)
        failure = self._failures.get(channel_id)
        if failure and failure.log_path:
            return _tail_file(failure.log_path, lines)
        return []

    def start_preview(self, channel_id: str, config: ChannelConfig) -> HlsPreviewInfo:
        """
        Launch an HLS preview FFmpeg process for *channel_id*.

        - Raises RuntimeError if preview.input_mode == "disabled".
        - Raises RuntimeError if a preview is already running.
        - Cleans old .ts files and playlist in the output directory first.
        - stderr goes to a timestamped log file.
        """
        input_mode = getattr(config.preview, "input_mode", "direct_capture")
        if input_mode == "disabled":
            raise RuntimeError(
                f"Preview for channel '{channel_id}' is disabled "
                "(preview.input_mode = 'disabled' in channel config). "
                "This is typically set on systems with a single capture device "
                "that is already owned by the recording process."
            )
        if input_mode == "from_recording_output":
            raise RuntimeError(
                "preview.input_mode = 'from_recording_output' is not yet implemented."
            )

        if self.is_running(channel_id):
            raise RuntimeError(
                f"HLS preview for channel '{channel_id}' is already running."
            )

        # Clear any previous failure record when starting fresh.
        self._failures.pop(channel_id, None)

        output_dir = self._output_dir(channel_id)
        self._clean_output_dir(output_dir)

        cmd = build_hls_preview_command(config, output_dir)
        log_path = self._new_log_path(channel_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"[{now_iso}] HLS PREVIEW COMMAND: {format_command_for_log(cmd)}\n")
            lf.write(f"[{now_iso}] STARTING\n")

        logger.info(
            "[hls-preview][%s] Starting: %s", channel_id, format_command_for_log(cmd)
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

        started_at = datetime.now(timezone.utc)
        info = HlsPreviewInfo(
            channel_id=channel_id,
            pid=process.pid,
            log_path=log_path,
            output_dir=output_dir,
            started_at=started_at,
            process=process,
            health=PreviewHealth.HEALTHY,
        )
        self._previews[channel_id] = info

        logger.info(
            "[hls-preview][%s] Started — PID %d, log: %s, output: %s",
            channel_id, process.pid, log_path, output_dir,
        )
        return info

    def stop_preview(self, channel_id: str) -> bool:
        """
        Stop the HLS preview process for *channel_id*.

        Returns True if a process was stopped, False if not running.
        Also clears any stored failure record.
        Uses SIGTERM → timeout → SIGKILL.
        """
        self._reap_if_dead(channel_id)
        # Clear failure record on explicit stop.
        self._failures.pop(channel_id, None)

        info = self._previews.get(channel_id)
        if info is None:
            logger.info("[hls-preview][%s] Stop requested but not running.", channel_id)
            return False

        pid = info.pid
        timeout = get_settings().stop_timeout_seconds
        logger.info(
            "[hls-preview][%s] Stopping PID %d (timeout=%ds).",
            channel_id, pid, timeout,
        )

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
                    "[hls-preview][%s] SIGTERM timeout — sending SIGKILL to PID %d.",
                    channel_id, pid,
                )
                info.process.kill()
                info.process.wait(timeout=5)
        except Exception as exc:
            logger.error(
                "[hls-preview][%s] Error stopping process: %s", channel_id, exc
            )

        del self._previews[channel_id]
        logger.info("[hls-preview][%s] Stopped (PID %d).", channel_id, pid)
        return True

    def preview_status(self, channel_id: str) -> dict:
        """
        Return a status dict for *channel_id*.

        Keys: running, pid, started_at, playlist_url, health,
              playlist_ready, startup_status, stderr_tail, failed_reason
        """
        self._reap_if_dead(channel_id)
        info = self._previews.get(channel_id)

        if info is None:
            failure = self._failures.get(channel_id)
            if failure:
                return {
                    "running": False,
                    "pid": None,
                    "started_at": None,
                    "playlist_url": None,
                    "health": PreviewHealth.DOWN,
                    "playlist_ready": False,
                    "startup_status": "failed",
                    "stderr_tail": _tail_file(failure.log_path, 50) if failure.log_path else [],
                    "failed_reason": failure.reason,
                }
            return {
                "running": False,
                "pid": None,
                "started_at": None,
                "playlist_url": None,
                "health": PreviewHealth.UNKNOWN,
                "playlist_ready": False,
                "startup_status": "stopped",
                "stderr_tail": [],
                "failed_reason": None,
            }

        playlist = info.output_dir / "index.m3u8"
        ready = _playlist_has_segment(playlist)
        startup_status = "running" if ready else "starting"

        return {
            "running": True,
            "pid": info.pid,
            "started_at": info.started_at,
            "playlist_url": self._playlist_api_url(channel_id),
            "health": info.health,
            "playlist_ready": ready,
            "startup_status": startup_status,
            "stderr_tail": _tail_file(info.log_path, 50),
            "failed_reason": None,
        }

    def get_output_dir(self, channel_id: str) -> Path:
        """Return the HLS output directory path (always valid, may not exist yet)."""
        return self._output_dir(channel_id)

    # ── Watchdog ──────────────────────────────────────────────────────────

    def check_all(self) -> None:
        """
        Called by the HLS preview watchdog loop.

        - Checks liveness — marks DOWN if the process has exited.
        - Checks startup timeout — kills and marks "failed" if no playlist after timeout.
        - Does NOT auto-restart (keeps recording pipeline unaffected).
        """
        for channel_id in list(self._previews):
            info = self._previews.get(channel_id)
            if info is None:
                continue
            if not info.is_alive():
                logger.warning(
                    "[hls-preview][%s] Process PID %d exited — marking DOWN.",
                    channel_id, info.pid,
                )
                info.mark_down()
                # _reap_if_dead will record failure if needed on next status check.
                self._reap_if_dead(channel_id)
            else:
                self._check_startup_timeout(channel_id)
                if channel_id in self._previews:
                    info.mark_healthy()

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _clean_output_dir(output_dir: Path) -> None:
        """
        Remove old HLS segments and playlist from *output_dir* before starting.

        Creates the directory if it does not exist.
        Only removes *.ts and *.m3u8 files — never the directory itself.
        Never raises on permission errors (logs a warning instead).
        """
        try:
            if output_dir.exists():
                for f in output_dir.iterdir():
                    if f.suffix in (".ts", ".m3u8") and f.is_file():
                        try:
                            f.unlink()
                        except OSError as exc:
                            logger.warning(
                                "[hls-preview] Could not remove %s: %s", f, exc
                            )
            else:
                output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[hls-preview] Could not clean output dir %s: %s", output_dir, exc
            )


# ─── Module-level singleton ────────────────────────────────────────────────────

_hls_preview_manager: Optional[HlsPreviewManager] = None


def get_hls_preview_manager() -> HlsPreviewManager:
    """Return the application-wide HlsPreviewManager singleton."""
    global _hls_preview_manager
    if _hls_preview_manager is None:
        _hls_preview_manager = HlsPreviewManager()
    return _hls_preview_manager


# ─── HLS Preview watchdog loop (independent asyncio Task) ─────────────────────

async def run_hls_preview_watchdog_loop() -> None:
    """
    Independent asyncio Task — runs its own interval loop.

    Checks all running HLS previews at the watchdog interval.
    Uses asyncio.to_thread to avoid blocking the event loop.
    """
    settings = get_settings()
    interval = settings.watchdog_interval_seconds
    logger.info("HLS preview watchdog: started (interval=%ds).", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_run_hls_preview_watchdog_sync)
        except Exception:
            logger.exception("HLS preview watchdog: unexpected error.")


def _run_hls_preview_watchdog_sync() -> None:
    """Synchronous body of the HLS preview watchdog — safe to run in a thread."""
    get_hls_preview_manager().check_all()

