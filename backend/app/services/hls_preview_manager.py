"""
HLS Preview Process Manager — Phase 5 / Phase 9 / Phase 10.

Manages a lightweight FFmpeg HLS-output preview process per channel.
Completely isolated from the recording pipeline.

Architecture
────────────
Each channel that has preview started via the API gets:

1. A separate FFmpeg subprocess that either:
   a) direct_capture: reads from the same capture device as recording, or
   b) from_recording_output: reads a completed segment from 1_record / 2_chunks,
      loops it at real-time speed, and switches to a newer file whenever the
      watchdog detects one.

2. The API layer serves index.m3u8 and .ts segments via FileResponse
   endpoints that are protected with JWT auth (any role can view).

3. A light watchdog that:
   - marks the preview DOWN if the process exits unexpectedly.
   - for from_recording_output mode: restarts with a newer completed segment
     whenever one appears, giving a "rolling delayed preview" without ever
     touching the capture device.

Isolation guarantees
────────────────────
- HlsPreviewManager is a completely separate singleton from ProcessManager.
- A preview crash NEVER touches the recording state.
- The recording watchdog and restart logic are not involved.
- Preview watchdog only marks DOWN; it does NOT auto-restart direct_capture mode.

Phase 9 additions
────────────────────
- playlist_ready: True only when index.m3u8 exists with at least one segment.
- startup_status: "stopped" | "starting" | "running" | "failed"
- startup timeout: if index.m3u8 never appears within
  preview_startup_timeout_seconds, the preview is marked "failed".
- failed_reason: last failure message stored so the UI can display it.
- get_log_tail(): expose preview FFmpeg stderr for in-browser admin view.

Phase 10 additions
────────────────────
- from_recording_output mode: preview reads completed segment files so the
  capture device is never opened by the preview process.
- _find_latest_usable_segment() / _find_newer_segment() module helpers.
- Pending mode: if no segment is available at start time, preview is queued
  and the watchdog starts it automatically when recording produces its first
  completed file.
- Watchdog switches to a newer completed segment as soon as one appears.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config.settings import get_settings, resolve_channel_path
from ..models.schemas import ChannelConfig, PreviewHealth
from ..utils import utc_now
from .ffmpeg_builder import (
    build_hls_preview_command,
    build_hls_preview_from_file_command,
    format_command_for_log,
)

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


def _safe_mtime(p: Path) -> float:
    """Return the mtime of *p*, or 0.0 on any OS error."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _find_latest_usable_segment(record_dir: Path, chunks_dir: Path) -> Optional[Path]:
    """
    Find the latest completed .mp4 segment suitable for file-based preview.

    Strategy:
    - In *record_dir*: all ``.mp4`` files **except** the most recently modified
      one (which is the segment currently being written by FFmpeg recording).
    - If nothing is found in *record_dir*, fall back to the latest file in
      *chunks_dir* (files moved from ``1_record`` after completion).

    Returns ``None`` if no usable segment exists yet (e.g. recording just
    started and the first segment is still being written).
    """
    # Check record_dir — the newest file is being written; skip it.
    try:
        if record_dir.exists():
            mp4s = sorted(record_dir.glob("*.mp4"), key=_safe_mtime)
            completed = mp4s[:-1]  # all except the newest (currently recording)
            if completed:
                return completed[-1]  # most recent completed segment
    except OSError:
        pass

    # Fall back to chunks_dir (completed segments that have been moved there).
    try:
        if chunks_dir.exists():
            mp4s = sorted(chunks_dir.glob("*.mp4"), key=_safe_mtime)
            if mp4s:
                return mp4s[-1]
    except OSError:
        pass

    return None


def _find_newer_segment(
    current_file: Path,
    record_dir: Path,
    chunks_dir: Path,
) -> Optional[Path]:
    """
    Return a completed segment file that is newer than *current_file*, or
    ``None`` if no newer segment is available yet.
    """
    current_mtime = _safe_mtime(current_file) if current_file.exists() else 0.0

    # Check record_dir (skip the currently-recording newest file).
    try:
        if record_dir.exists():
            mp4s = sorted(record_dir.glob("*.mp4"), key=_safe_mtime)
            completed = mp4s[:-1]
            for f in reversed(completed):
                if _safe_mtime(f) > current_mtime and f != current_file:
                    return f
    except OSError:
        pass

    # Check chunks_dir.
    try:
        if chunks_dir.exists():
            for f in sorted(chunks_dir.glob("*.mp4"), key=_safe_mtime, reverse=True):
                if _safe_mtime(f) > current_mtime and f != current_file:
                    return f
    except OSError:
        pass

    return None


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
    # Phase 10 — file-based preview mode
    # "direct_capture" or "from_recording_output"
    input_mode: str = field(default="direct_capture")
    # Path to the segment file currently being looped (from_recording_output only)
    source_file: Optional[Path] = field(default=None)

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
        # Phase 10: from_recording_output pending and config stores.
        # _pending_file_mode: channel_id → config for channels waiting for first
        #   segment to appear (preview requested but no file available yet).
        self._pending_file_mode: dict[str, ChannelConfig] = {}
        # _file_mode_configs: channel_id → config for running file-based previews
        #   (needed so the watchdog can find newer segments and restart).
        self._file_mode_configs: dict[str, ChannelConfig] = {}

    # ── Internal helpers ──────────────────────────────────────────────────

    def _new_log_path(self, channel_id: str) -> Path:
        """Return a timestamped log file path for the HLS preview process."""
        settings = get_settings()
        log_dir = settings.logs_dir / "channels" / channel_id
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = utc_now().strftime("%Y%m%d-%H%M%S")
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
                    failed_at=utc_now(),
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
        elapsed = (utc_now() - info.started_at).total_seconds()
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
                failed_at=utc_now(),
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
        """Return True if a preview process is running OR pending for *channel_id*."""
        self._reap_if_dead(channel_id)
        return channel_id in self._previews or channel_id in self._pending_file_mode

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

    def start_preview(
        self, channel_id: str, config: ChannelConfig
    ) -> Optional[HlsPreviewInfo]:
        """
        Launch an HLS preview FFmpeg process for *channel_id*.

        Returns the ``HlsPreviewInfo`` when a process is started immediately,
        or ``None`` when the channel is queued in pending mode (i.e.
        ``input_mode == "from_recording_output"`` but no completed segment is
        available yet — the watchdog will start it automatically).

        Raises:
          RuntimeError  if ``preview.input_mode == "disabled"``
          RuntimeError  if a preview (or pending request) is already active.
        """
        input_mode = getattr(config.preview, "input_mode", "direct_capture")
        if input_mode == "disabled":
            raise RuntimeError(
                f"Preview for channel '{channel_id}' is disabled "
                "(preview.input_mode = 'disabled' in channel config). "
                "On systems with a single capture device already owned by "
                "recording, set input_mode = 'from_recording_output' instead."
            )

        if self.is_running(channel_id):
            raise RuntimeError(
                f"HLS preview for channel '{channel_id}' is already running."
            )

        # Clear any previous failure record when starting fresh.
        self._failures.pop(channel_id, None)

        # ── from_recording_output mode ────────────────────────────────────
        if input_mode == "from_recording_output":
            record_dir = resolve_channel_path(config.paths.record_dir)
            chunks_dir = resolve_channel_path(config.paths.chunks_dir)
            source_file = _find_latest_usable_segment(record_dir, chunks_dir)
            if source_file is None:
                # No completed segment yet — queue as pending.
                self._pending_file_mode[channel_id] = config
                self._file_mode_configs[channel_id] = config
                logger.info(
                    "[hls-preview][%s] from_recording_output: no completed segment "
                    "available yet; preview queued — watchdog will start it when "
                    "recording produces its first segment.",
                    channel_id,
                )
                return None
            return self._start_from_file(channel_id, config, source_file)

        # ── direct_capture mode (original behavior) ───────────────────────
        output_dir = self._output_dir(channel_id)
        self._clean_output_dir(output_dir)

        cmd = build_hls_preview_command(config, output_dir)
        log_path = self._new_log_path(channel_id)

        now_iso = utc_now().isoformat()
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

        started_at = utc_now()
        info = HlsPreviewInfo(
            channel_id=channel_id,
            pid=process.pid,
            log_path=log_path,
            output_dir=output_dir,
            started_at=started_at,
            process=process,
            health=PreviewHealth.HEALTHY,
            input_mode="direct_capture",
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

        Returns True if a process (or pending request) was stopped, False if
        not running.
        Also clears any stored failure record and pending state.
        Uses SIGTERM → timeout → SIGKILL.
        """
        self._reap_if_dead(channel_id)
        # Clear failure record and pending/file-mode state on explicit stop.
        self._failures.pop(channel_id, None)
        # Phase 10: clear pending / file-mode configs
        was_pending = channel_id in self._pending_file_mode
        self._pending_file_mode.pop(channel_id, None)
        self._file_mode_configs.pop(channel_id, None)

        info = self._previews.get(channel_id)
        if info is None:
            if was_pending:
                logger.info("[hls-preview][%s] Pending preview cancelled.", channel_id)
                return True
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
            # Phase 10: pending mode (waiting for first segment to appear)
            if channel_id in self._pending_file_mode:
                return {
                    "running": False,
                    "pid": None,
                    "started_at": None,
                    "playlist_url": None,
                    "health": PreviewHealth.UNKNOWN,
                    "playlist_ready": False,
                    "startup_status": "starting",
                    "stderr_tail": [],
                    "failed_reason": None,
                }

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

    # ── File-based preview helpers (from_recording_output) ────────────────

    def _start_from_file(
        self, channel_id: str, config: ChannelConfig, source_file: Path
    ) -> HlsPreviewInfo:
        """
        Start a file-based HLS preview process for *channel_id*.

        Cleans the HLS output directory, then launches FFmpeg to loop
        *source_file* at real-time speed and produce HLS output.
        """
        output_dir = self._output_dir(channel_id)
        self._clean_output_dir(output_dir)

        cmd = build_hls_preview_from_file_command(config, source_file, output_dir)
        log_path = self._new_log_path(channel_id)

        now_iso = utc_now().isoformat()
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(
                f"[{now_iso}] HLS PREVIEW FROM FILE: {source_file}\n"
                f"[{now_iso}] COMMAND: {format_command_for_log(cmd)}\n"
                f"[{now_iso}] STARTING\n"
            )

        logger.info(
            "[hls-preview][%s] from_recording_output: starting from %s",
            channel_id, source_file.name,
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
        info = HlsPreviewInfo(
            channel_id=channel_id,
            pid=process.pid,
            log_path=log_path,
            output_dir=output_dir,
            started_at=started_at,
            process=process,
            health=PreviewHealth.HEALTHY,
            input_mode="from_recording_output",
            source_file=source_file,
        )
        self._previews[channel_id] = info
        self._file_mode_configs[channel_id] = config

        logger.info(
            "[hls-preview][%s] from_recording_output: PID %d looping %s",
            channel_id, process.pid, source_file.name,
        )
        return info

    def _switch_to_newer_file(
        self, channel_id: str, config: ChannelConfig, new_file: Path
    ) -> None:
        """
        Kill the current file-based preview process and restart with *new_file*.

        Called by the watchdog when a newer completed segment is detected.
        """
        info = self._previews.get(channel_id)
        if info:
            logger.info(
                "[hls-preview][%s] from_recording_output: newer segment %s found, "
                "switching from %s.",
                channel_id,
                new_file.name,
                info.source_file.name if info.source_file else "?",
            )
            try:
                info.process.kill()
                info.process.wait(timeout=5)
            except Exception as exc:
                logger.warning(
                    "[hls-preview][%s] Error killing process for file switch: %s",
                    channel_id, exc,
                )
            del self._previews[channel_id]

        try:
            self._start_from_file(channel_id, config, new_file)
        except Exception as exc:
            logger.error(
                "[hls-preview][%s] Failed to start with new file %s: %s",
                channel_id, new_file.name, exc,
            )
            self._failures[channel_id] = HlsPreviewFailure(
                reason=f"Failed to restart preview with {new_file.name}: {exc}",
                log_path=None,
                failed_at=utc_now(),
            )

    def _handle_file_mode_process_exit(
        self, channel_id: str, config: ChannelConfig, old_info: HlsPreviewInfo
    ) -> None:
        """
        Called when a file-based preview process exits.

        Looks for a newer completed segment and restarts, or falls back to
        pending mode if none is available.
        """
        rc = old_info.process.poll()
        logger.info(
            "[hls-preview][%s] from_recording_output: FFmpeg finished "
            "(code=%s), searching for next segment.",
            channel_id, rc,
        )
        del self._previews[channel_id]

        record_dir = resolve_channel_path(config.paths.record_dir)
        chunks_dir = resolve_channel_path(config.paths.chunks_dir)
        # Try to find any newer segment; fall back to the same or latest available.
        if old_info.source_file:
            next_file = _find_newer_segment(old_info.source_file, record_dir, chunks_dir)
        else:
            next_file = None
        if next_file is None:
            next_file = _find_latest_usable_segment(record_dir, chunks_dir)

        if next_file:
            try:
                self._start_from_file(channel_id, config, next_file)
            except Exception as exc:
                logger.error(
                    "[hls-preview][%s] from_recording_output: restart failed: %s",
                    channel_id, exc,
                )
                self._failures[channel_id] = HlsPreviewFailure(
                    reason=f"Restart after file end failed: {exc}",
                    log_path=old_info.log_path,
                    failed_at=utc_now(),
                )
        else:
            # No segment available — go back to pending.
            logger.info(
                "[hls-preview][%s] from_recording_output: no segment after exit, "
                "going back to pending.",
                channel_id,
            )
            self._pending_file_mode[channel_id] = config

    # ── Watchdog ──────────────────────────────────────────────────────────

    def check_all(self) -> None:
        """
        Called by the HLS preview watchdog loop.

        1. Pending channels (from_recording_output, waiting for first segment):
           Try to find a segment and start the process.

        2. Running channels:
           a. direct_capture: marks DOWN if the process has exited; checks
              startup timeout.  Does NOT auto-restart.
           b. from_recording_output: if a newer segment is available, switch
              to it.  If the process has exited, restart with the next segment
              (or return to pending if none available).
        """
        # ── Handle pending file-mode channels ─────────────────────────────
        for channel_id, config in list(self._pending_file_mode.items()):
            record_dir = resolve_channel_path(config.paths.record_dir)
            chunks_dir = resolve_channel_path(config.paths.chunks_dir)
            source_file = _find_latest_usable_segment(record_dir, chunks_dir)
            if source_file is not None:
                del self._pending_file_mode[channel_id]
                logger.info(
                    "[hls-preview][%s] from_recording_output: segment %s now "
                    "available — starting preview.",
                    channel_id, source_file.name,
                )
                try:
                    self._start_from_file(channel_id, config, source_file)
                except Exception as exc:
                    logger.error(
                        "[hls-preview][%s] from_recording_output: failed to start: %s",
                        channel_id, exc,
                    )
                    self._failures[channel_id] = HlsPreviewFailure(
                        reason=f"Failed to start from file: {exc}",
                        log_path=None,
                        failed_at=utc_now(),
                    )

        # ── Handle running previews ────────────────────────────────────────
        for channel_id in list(self._previews):
            info = self._previews.get(channel_id)
            if info is None:
                continue

            if not info.is_alive():
                if info.input_mode == "from_recording_output":
                    config = self._file_mode_configs.get(channel_id)
                    if config:
                        self._handle_file_mode_process_exit(channel_id, config, info)
                    else:
                        # No config stored — fall back to generic failure handling.
                        info.mark_down()
                        self._reap_if_dead(channel_id)
                else:
                    # direct_capture: mark down, record failure if no playlist.
                    logger.warning(
                        "[hls-preview][%s] Process PID %d exited — marking DOWN.",
                        channel_id, info.pid,
                    )
                    info.mark_down()
                    self._reap_if_dead(channel_id)
            else:
                if info.input_mode == "from_recording_output":
                    # Check for a newer completed segment.
                    config = self._file_mode_configs.get(channel_id)
                    if config and info.source_file:
                        record_dir = resolve_channel_path(config.paths.record_dir)
                        chunks_dir = resolve_channel_path(config.paths.chunks_dir)
                        newer = _find_newer_segment(
                            info.source_file, record_dir, chunks_dir
                        )
                        if newer:
                            self._switch_to_newer_file(channel_id, config, newer)
                            continue
                else:
                    # direct_capture: check startup timeout.
                    self._check_startup_timeout(channel_id)

                if channel_id in self._previews:
                    self._previews[channel_id].mark_healthy()

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

