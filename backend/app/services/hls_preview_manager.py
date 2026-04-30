"""
HLS Preview Process Manager — Phase 5.

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


# ─── HLS Preview manager ──────────────────────────────────────────────────────

class HlsPreviewManager:
    """
    Singleton that owns all FFmpeg HLS preview subprocesses.

    Completely independent of ProcessManager — no shared state.
    """

    def __init__(self) -> None:
        self._previews: dict[str, HlsPreviewInfo] = {}

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
        """Remove a channel entry if its preview process has already exited."""
        info = self._previews.get(channel_id)
        if info and not info.is_alive():
            logger.info(
                "[hls-preview][%s] Process PID %d exited (returncode=%s).",
                channel_id,
                info.pid,
                info.process.returncode,
            )
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

    def start_preview(self, channel_id: str, config: ChannelConfig) -> HlsPreviewInfo:
        """
        Launch an HLS preview FFmpeg process for *channel_id*.

        - Cleans old .ts files and playlist in the output directory first.
        - Raises RuntimeError if a preview is already running.
        - stderr goes to a timestamped log file.
        """
        if self.is_running(channel_id):
            raise RuntimeError(
                f"HLS preview for channel '{channel_id}' is already running."
            )

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
        Uses SIGTERM → timeout → SIGKILL.
        """
        self._reap_if_dead(channel_id)
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

        Keys: running, pid, started_at, playlist_url, health
        """
        self._reap_if_dead(channel_id)
        info = self._previews.get(channel_id)
        if info is None:
            return {
                "running": False,
                "pid": None,
                "started_at": None,
                "playlist_url": None,
                "health": PreviewHealth.UNKNOWN,
            }
        return {
            "running": True,
            "pid": info.pid,
            "started_at": info.started_at,
            "playlist_url": self._playlist_api_url(channel_id),
            "health": info.health,
        }

    def get_output_dir(self, channel_id: str) -> Path:
        """Return the HLS output directory path (always valid, may not exist yet)."""
        return self._output_dir(channel_id)

    # ── Watchdog ──────────────────────────────────────────────────────────

    def check_all(self) -> None:
        """
        Called by the HLS preview watchdog loop.

        Only checks liveness — marks DOWN if the process has exited.
        Does NOT auto-restart (keeps recording pipeline unaffected).
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
                del self._previews[channel_id]
            else:
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
