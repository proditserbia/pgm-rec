"""
Phase 2 unit tests.

Covers:
- PreviewConfig: fps field default
- build_preview_command(): structure + flags
- _FrameReader: JPEG parsing from a byte stream
- PreviewManager: start_preview raises on duplicate; stop; status; get_latest_frame
- Preview watchdog (check_all): marks DOWN when process exits
- PreviewStatusResponse schema
- PreviewHealth enum
- Channel configs: rts2, rts3, rts_test parse without errors
- Multi-channel isolation: each channel has independent preview state
"""
from __future__ import annotations

import io
import json
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from app.models.schemas import (
    ChannelConfig,
    PreviewConfig,
    PreviewHealth,
    PreviewStatusResponse,
)
from app.services.ffmpeg_builder import build_preview_command
from app.services.preview_manager import (
    PreviewInfo,
    PreviewManager,
    _FrameReader,
    get_preview_manager,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _load_channel_config(filename: str) -> ChannelConfig:
    """Load a channel config JSON from the data/channels directory."""
    base = Path(__file__).parent.parent / "data" / "channels"
    data = (base / filename).read_text(encoding="utf-8")
    return ChannelConfig.model_validate_json(data)


def _make_config(channel_id: str = "rts_test_unit", port: int = 23099) -> ChannelConfig:
    """Build a minimal ChannelConfig for testing."""
    return ChannelConfig(
        id=channel_id,
        name="TEST",
        display_name="Test Channel",
        paths={
            "record_dir": "/tmp/test_record",
            "chunks_dir": "/tmp/test_chunks",
            "final_dir": "/tmp/test_final",
        },
        preview=PreviewConfig(enabled=True, port=port, scale="320:180", fps=5),
    )


def _make_dead_popen() -> MagicMock:
    """Return a mock Popen that looks like it has already exited."""
    p = MagicMock(spec=subprocess.Popen)
    p.poll.return_value = 0        # exited with code 0
    p.returncode = 0
    p.stdout = io.BytesIO(b"")    # empty stdout
    return p


def _make_alive_popen(stdout_data: bytes = b"") -> MagicMock:
    """Return a mock Popen that looks like it is still running."""
    p = MagicMock(spec=subprocess.Popen)
    p.poll.return_value = None     # still running
    p.returncode = None
    p.stdout = io.BytesIO(stdout_data)
    return p


# ─── PreviewConfig defaults ───────────────────────────────────────────────────

def test_preview_config_defaults():
    pc = PreviewConfig()
    assert pc.fps == 5
    assert pc.scale == "320:180"
    assert pc.enabled is False


def test_preview_config_fps_override():
    pc = PreviewConfig(fps=10)
    assert pc.fps == 10


# ─── PreviewHealth enum ───────────────────────────────────────────────────────

def test_preview_health_values():
    assert set(PreviewHealth) == {
        PreviewHealth.HEALTHY,
        PreviewHealth.DOWN,
        PreviewHealth.UNKNOWN,
    }


# ─── Channel config files parse correctly ────────────────────────────────────

def test_rts1_config_parses():
    config = _load_channel_config("rts1.json")
    assert config.id == "rts1"
    assert config.preview.port == 23001


def test_rts2_config_parses():
    config = _load_channel_config("rts2.json")
    assert config.id == "rts2"
    assert config.preview.port == 23002
    assert config.preview.fps == 5
    # Path string must reference the rts2 subdirectory (date-folder mode)
    assert "rts2" in config.paths.record_root
    assert config.paths.record_dir is None


def test_rts3_config_parses():
    config = _load_channel_config("rts3.json")
    assert config.id == "rts3"
    assert config.preview.port == 23003
    assert "rts3" in config.paths.record_root
    assert config.paths.record_dir is None


def test_rts_test_config_parses():
    config = _load_channel_config("rts_test.json")
    assert config.id == "rts_test"
    assert config.preview.port == 23004
    assert config.retention.days == 7


def test_channel_configs_all_unique_ids():
    ids = []
    for fn in ["rts1.json", "rts2.json", "rts3.json", "rts_test.json"]:
        ids.append(_load_channel_config(fn).id)
    assert len(ids) == len(set(ids)), "Channel IDs must be unique"


def test_channel_configs_all_unique_ports():
    ports = []
    for fn in ["rts1.json", "rts2.json", "rts3.json", "rts_test.json"]:
        ports.append(_load_channel_config(fn).preview.port)
    assert len(ports) == len(set(ports)), "Preview ports must be unique"


# ─── build_preview_command ────────────────────────────────────────────────────

def test_build_preview_command_structure():
    config = _make_config()
    cmd = build_preview_command(config)
    assert isinstance(cmd, list)
    assert len(cmd) > 0


def test_build_preview_command_no_audio():
    config = _make_config()
    cmd = build_preview_command(config)
    assert "-an" in cmd


def test_build_preview_command_mjpeg_output():
    config = _make_config()
    cmd = build_preview_command(config)
    assert "-f" in cmd
    f_idx = [i for i, x in enumerate(cmd) if x == "-f"]
    formats = [cmd[i + 1] for i in f_idx if i + 1 < len(cmd)]
    assert "mjpeg" in formats


def test_build_preview_command_pipe_output():
    config = _make_config()
    cmd = build_preview_command(config)
    assert "pipe:1" in cmd


def test_build_preview_command_scale_filter():
    config = _make_config()
    cmd = build_preview_command(config)
    vf_idx = cmd.index("-vf")
    vf_value = cmd[vf_idx + 1]
    assert "scale=320:180" in vf_value
    assert "fps=5" in vf_value


def test_build_preview_command_no_stream_segment():
    config = _make_config()
    cmd = build_preview_command(config)
    assert "stream_segment" not in cmd


def test_build_preview_command_uses_ffmpeg_path():
    config = _make_config()
    config.ffmpeg_path = "/custom/ffmpeg"
    cmd = build_preview_command(config)
    assert cmd[0] == "/custom/ffmpeg"


# ─── _FrameReader ─────────────────────────────────────────────────────────────

def _build_fake_mjpeg(*frames: bytes) -> bytes:
    """Concatenate multiple JPEG blobs into a raw MJPEG byte string."""
    return b"".join(frames)


def _make_jpeg(content: bytes = b"fake-content") -> bytes:
    """Return a minimal valid JPEG (SOI + content + EOI)."""
    return b"\xff\xd8" + content + b"\xff\xd9"


def test_frame_reader_parses_single_frame():
    jpeg = _make_jpeg(b"hello")
    process = _make_alive_popen(jpeg)
    # Make poll() return None first, then 0 after the data is read
    process.poll.side_effect = [None, None, 0]

    reader = _FrameReader(process, "test")
    reader.run()

    assert reader.latest_frame == jpeg


def test_frame_reader_parses_multiple_frames():
    f1 = _make_jpeg(b"frame1")
    f2 = _make_jpeg(b"frame2")
    f3 = _make_jpeg(b"frame3")
    data = f1 + f2 + f3
    process = _make_alive_popen(data)
    process.poll.side_effect = [None, None, 0]

    reader = _FrameReader(process, "test")
    reader.run()

    assert reader.latest_frame == f3  # only the latest is retained
    assert reader.frame_count == 3


def test_frame_reader_handles_empty_stdout():
    process = _make_alive_popen(b"")
    process.poll.side_effect = [None, 0]

    reader = _FrameReader(process, "test")
    reader.run()

    assert reader.latest_frame is None


def test_frame_reader_handles_garbage_before_jpeg():
    garbage = b"\x00\x01\x02garbage\x03\x04"
    jpeg = _make_jpeg(b"real-frame")
    process = _make_alive_popen(garbage + jpeg)
    process.poll.side_effect = [None, None, 0]

    reader = _FrameReader(process, "test")
    reader.run()

    assert reader.latest_frame == jpeg


# ─── PreviewManager ───────────────────────────────────────────────────────────

def _make_manager() -> PreviewManager:
    return PreviewManager()


def _inject_preview(
    manager: PreviewManager, channel_id: str, alive: bool = True
) -> PreviewInfo:
    """Insert a fake PreviewInfo directly into the manager."""
    process = _make_alive_popen() if alive else _make_dead_popen()
    reader = MagicMock(spec=_FrameReader)
    reader.latest_frame = None
    info = PreviewInfo(
        channel_id=channel_id,
        pid=12345,
        log_path=Path("/tmp/test.log"),
        started_at=datetime.now(timezone.utc),
        process=process,
        reader=reader,
        health=PreviewHealth.HEALTHY if alive else PreviewHealth.DOWN,
    )
    manager._previews[channel_id] = info
    return info


def test_preview_manager_is_running_false_initially():
    pm = _make_manager()
    assert pm.is_running("rts1") is False


def test_preview_manager_is_running_true_after_inject():
    pm = _make_manager()
    _inject_preview(pm, "rts1")
    assert pm.is_running("rts1") is True


def test_preview_manager_reap_removes_dead_process():
    pm = _make_manager()
    _inject_preview(pm, "rts1", alive=False)
    pm._reap_if_dead("rts1")
    assert "rts1" not in pm._previews


def test_preview_manager_get_latest_frame_none_when_not_running():
    pm = _make_manager()
    assert pm.get_latest_frame("rts1") is None


def test_preview_manager_get_latest_frame_delegates_to_reader():
    pm = _make_manager()
    info = _inject_preview(pm, "rts1")
    info.reader.latest_frame = b"\xff\xd8test\xff\xd9"
    assert pm.get_latest_frame("rts1") == b"\xff\xd8test\xff\xd9"


def test_preview_manager_start_raises_on_duplicate(tmp_path):
    pm = _make_manager()
    _inject_preview(pm, "rts1")
    config = _make_config("rts1")
    with pytest.raises(RuntimeError, match="already running"):
        pm.start_preview("rts1", config)


def test_preview_manager_stop_returns_false_when_not_running():
    pm = _make_manager()
    assert pm.stop_preview("rts1") is False


def test_preview_manager_stop_returns_true_and_removes(tmp_path):
    pm = _make_manager()
    info = _inject_preview(pm, "rts1")
    # Patch process.wait so it doesn't block
    info.process.wait.return_value = 0
    result = pm.stop_preview("rts1")
    assert result is True
    assert not pm.is_running("rts1")


def test_preview_manager_preview_status_not_running():
    pm = _make_manager()
    status = pm.preview_status("rts1")
    assert status["running"] is False
    assert status["pid"] is None
    assert status["health"] == PreviewHealth.UNKNOWN


def test_preview_manager_preview_status_running():
    pm = _make_manager()
    _inject_preview(pm, "rts1")
    status = pm.preview_status("rts1")
    assert status["running"] is True
    assert status["pid"] == 12345
    assert "/rts1/" in status["stream_url"]


def test_preview_manager_check_all_marks_down_dead_process():
    pm = _make_manager()
    _inject_preview(pm, "rts1", alive=False)
    # Re-add it (reap_if_dead would remove it, we need it to be alive in _previews)
    process = _make_dead_popen()
    reader = MagicMock()
    info = PreviewInfo(
        channel_id="rts1",
        pid=12345,
        log_path=Path("/tmp/test.log"),
        started_at=datetime.now(timezone.utc),
        process=process,
        reader=reader,
        health=PreviewHealth.HEALTHY,
    )
    pm._previews["rts1"] = info
    pm.check_all()
    # After check_all, the dead process should be removed
    assert not pm.is_running("rts1")


def test_preview_manager_check_all_keeps_healthy_alive():
    pm = _make_manager()
    _inject_preview(pm, "rts1", alive=True)
    pm.check_all()
    assert pm.is_running("rts1")
    assert pm.get_health("rts1") == PreviewHealth.HEALTHY


# ─── Multi-channel isolation ──────────────────────────────────────────────────

def test_multi_channel_preview_independent():
    """Starting/stopping preview on one channel doesn't affect another."""
    pm = _make_manager()
    _inject_preview(pm, "rts1")
    _inject_preview(pm, "rts2")

    # rts1 still running
    assert pm.is_running("rts1")
    assert pm.is_running("rts2")

    # Stop rts1
    info1 = pm._previews["rts1"]
    info1.process.wait.return_value = 0
    pm.stop_preview("rts1")

    assert not pm.is_running("rts1")
    assert pm.is_running("rts2")  # rts2 unaffected


def test_multi_channel_frames_are_separate():
    """Latest frame for each channel is stored independently."""
    pm = _make_manager()
    info1 = _inject_preview(pm, "rts1")
    info2 = _inject_preview(pm, "rts2")
    info1.reader.latest_frame = b"\xff\xd8frame1\xff\xd9"
    info2.reader.latest_frame = b"\xff\xd8frame2\xff\xd9"

    assert pm.get_latest_frame("rts1") != pm.get_latest_frame("rts2")
    assert pm.get_latest_frame("rts1") == b"\xff\xd8frame1\xff\xd9"
    assert pm.get_latest_frame("rts2") == b"\xff\xd8frame2\xff\xd9"


# ─── PreviewStatusResponse schema ─────────────────────────────────────────────

def test_preview_status_response_schema():
    r = PreviewStatusResponse(
        channel_id="rts1",
        running=True,
        pid=9999,
        stream_url="/api/v1/channels/rts1/preview/stream",
        health=PreviewHealth.HEALTHY,
    )
    assert r.channel_id == "rts1"
    assert r.running is True
    assert r.health == PreviewHealth.HEALTHY


def test_preview_status_response_defaults():
    r = PreviewStatusResponse(channel_id="rts2", running=False)
    assert r.pid is None
    assert r.stream_url is None
    assert r.health == PreviewHealth.UNKNOWN
