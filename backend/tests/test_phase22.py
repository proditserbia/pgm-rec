"""
Phase 22 unit tests — Broadcast-Safe Live Preview: hls_direct mode.

Covers:
- RecordingPreviewOutputConfig: mode field defaults to "udp"
- RecordingPreviewOutputConfig: mode accepts "udp", "hls_direct", "disabled"
- RecordingPreviewOutputConfig: hls_time, hls_list_size, hls_flags fields
- RecordingPreviewOutputConfig: hls field defaults
- build_ffmpeg_command: mode="udp" (default) → UDP path (unchanged)
- build_ffmpeg_command: mode="hls_direct" → HLS direct path
- build_ffmpeg_command: mode="disabled" (enabled=True) → plain recording (no preview)
- _build_recording_command_with_hls_direct: filter_complex present
- _build_recording_command_with_hls_direct: [main_v] and [prev_v] pads
- _build_recording_command_with_hls_direct: main output uses stream_segment
- _build_recording_command_with_hls_direct: preview output uses -f hls
- _build_recording_command_with_hls_direct: -hls_time from rpo.hls_time
- _build_recording_command_with_hls_direct: -hls_list_size from rpo.hls_list_size
- _build_recording_command_with_hls_direct: -hls_flags from rpo.hls_flags
- _build_recording_command_with_hls_direct: -hls_segment_filename ends in seg%05d.ts
- _build_recording_command_with_hls_direct: playlist ends in index.m3u8
- _build_recording_command_with_hls_direct: audio enabled
- _build_recording_command_with_hls_direct: audio disabled (-an)
- _build_recording_command_with_hls_direct: libx264 GOP flags
- _build_recording_command_with_hls_direct: h264_nvenc flags
- _build_recording_command_with_hls_direct: fail_safe_mode NVENC warning
- HlsPreviewManager._start_hls_direct: registers channel in _hls_direct_channels
- HlsPreviewManager._start_hls_direct: creates output directory
- HlsPreviewManager._start_hls_direct: raises RuntimeError when rpo is None
- HlsPreviewManager._start_hls_direct: raises RuntimeError when rpo.enabled=False
- HlsPreviewManager._start_hls_direct: raises RuntimeError when rpo.mode != "hls_direct"
- HlsPreviewManager.start_preview: hls_direct input_mode calls _start_hls_direct
- HlsPreviewManager.is_running: returns True when hls_direct channel registered
- HlsPreviewManager.stop_preview: deregisters hls_direct channel and returns True
- HlsPreviewManager.stop_preview: returns False when hls_direct not registered
- HlsPreviewManager.preview_status: hls_direct channel returns running=True
- HlsPreviewManager.preview_status: hls_direct playlist_ready=False when no m3u8
- HlsPreviewManager.preview_status: hls_direct playlist_ready=True when m3u8 ready
- HlsPreviewManager.preview_status: hls_direct startup_status "starting" vs "running"
- rts1.json Phase 22: mode="hls_direct"
- rts1.json Phase 22: hls_time=2
- rts1.json Phase 22: hls_list_size=5
- rts1.json Phase 22: hls_flags contains delete_segments and independent_segments
- rts1.json Phase 22: preview.input_mode="hls_direct"
- rts1.json Phase 22: build produces -f hls in command
- rts1.json Phase 22: build produces -hls_segment_filename in command
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models.schemas import (
    ChannelConfig,
    PreviewConfig,
    RecordingPreviewOutputConfig,
)
from app.services.ffmpeg_builder import (
    _build_recording_command_with_hls_direct,
    build_ffmpeg_command,
)
from app.services.hls_preview_manager import HlsPreviewManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_config(
    *,
    mode: str = "hls_direct",
    rpo_enabled: bool = True,
    rpo_kwargs: dict | None = None,
    input_mode: str = "hls_direct",
) -> ChannelConfig:
    kwargs: dict = {
        "enabled": rpo_enabled,
        "mode": mode,
        "video_codec": "libx264",
        "preset": "ultrafast",
        "tune": "zerolatency",
        "fail_safe_mode": False,
    }
    if rpo_kwargs:
        kwargs.update(rpo_kwargs)
    return ChannelConfig(
        id="rts1",
        name="RTS1",
        display_name="RTS1 Test",
        capture={"device_type": "dshow"},
        paths={
            "record_dir": "/tmp/rec",
            "chunks_dir": "/tmp/chunks",
            "final_dir": "/tmp/final",
        },
        recording_preview_output=RecordingPreviewOutputConfig(**kwargs),
        preview=PreviewConfig(input_mode=input_mode),
    )


def _load_rts1() -> ChannelConfig:
    base = Path(__file__).parent.parent / "data" / "channels"
    return ChannelConfig.model_validate_json((base / "rts1.json").read_text())


def _mock_process(returncode=None):
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 44444
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


# ---------------------------------------------------------------------------
# RecordingPreviewOutputConfig — mode field
# ---------------------------------------------------------------------------

def test_rpo_mode_default_is_udp():
    """mode must default to 'udp' for backward compatibility."""
    rpo = RecordingPreviewOutputConfig()
    assert rpo.mode == "udp"


def test_rpo_mode_accepts_hls_direct():
    rpo = RecordingPreviewOutputConfig(mode="hls_direct")
    assert rpo.mode == "hls_direct"


def test_rpo_mode_accepts_disabled():
    rpo = RecordingPreviewOutputConfig(mode="disabled")
    assert rpo.mode == "disabled"


def test_rpo_mode_accepts_udp_explicit():
    rpo = RecordingPreviewOutputConfig(mode="udp")
    assert rpo.mode == "udp"


# ---------------------------------------------------------------------------
# RecordingPreviewOutputConfig — HLS direct fields
# ---------------------------------------------------------------------------

def test_rpo_hls_time_default():
    rpo = RecordingPreviewOutputConfig()
    assert rpo.hls_time == 2


def test_rpo_hls_list_size_default():
    rpo = RecordingPreviewOutputConfig()
    assert rpo.hls_list_size == 5


def test_rpo_hls_flags_default():
    rpo = RecordingPreviewOutputConfig()
    assert "delete_segments" in rpo.hls_flags
    assert "append_list" in rpo.hls_flags
    assert "independent_segments" in rpo.hls_flags


def test_rpo_hls_fields_round_trip():
    rpo = RecordingPreviewOutputConfig(
        mode="hls_direct", hls_time=4, hls_list_size=10,
        hls_flags="delete_segments+independent_segments",
    )
    data = rpo.model_dump_json()
    rpo2 = RecordingPreviewOutputConfig.model_validate_json(data)
    assert rpo2.hls_time == 4
    assert rpo2.hls_list_size == 10
    assert rpo2.hls_flags == "delete_segments+independent_segments"


# ---------------------------------------------------------------------------
# build_ffmpeg_command — routing by mode
# ---------------------------------------------------------------------------

def test_build_ffmpeg_command_udp_mode_produces_mpegts(tmp_path):
    """mode='udp' (default) must produce a UDP output with mpegts format."""
    cfg = _base_config(mode="udp", input_mode="from_udp")
    cmd = build_ffmpeg_command(cfg)
    f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
    formats = [cmd[i + 1] for i in f_indices]
    assert "mpegts" in formats


def test_build_ffmpeg_command_hls_direct_mode_produces_hls():
    """mode='hls_direct' must produce an HLS output (-f hls)."""
    cfg = _base_config(mode="hls_direct")
    cmd = build_ffmpeg_command(cfg)
    f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
    formats = [cmd[i + 1] for i in f_indices]
    assert "hls" in formats


def test_build_ffmpeg_command_disabled_mode_no_preview_output():
    """mode='disabled' with enabled=True must produce a plain recording (no preview)."""
    cfg = _base_config(mode="disabled")
    cmd = build_ffmpeg_command(cfg)
    # Plain recording: uses -vf chain, not -filter_complex
    assert "-filter_complex" not in cmd
    assert "-vf" in cmd


def test_build_ffmpeg_command_rpo_none_no_filter_complex():
    """When recording_preview_output is None, plain command must be built."""
    cfg = ChannelConfig(
        id="rts1",
        name="RTS1",
        display_name="RTS1 Test",
        capture={"device_type": "dshow"},
        paths={"record_dir": "/tmp/rec", "chunks_dir": "/tmp/chunks", "final_dir": "/tmp/final"},
    )
    cmd = build_ffmpeg_command(cfg)
    assert "-filter_complex" not in cmd


# ---------------------------------------------------------------------------
# _build_recording_command_with_hls_direct — structure
# ---------------------------------------------------------------------------

def test_hls_direct_filter_complex_present():
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-filter_complex" in cmd


def test_hls_direct_filter_complex_has_main_v_pad():
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    fc_idx = cmd.index("-filter_complex")
    fc_val = cmd[fc_idx + 1]
    assert "[main_v]" in fc_val


def test_hls_direct_filter_complex_has_prev_v_pad():
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    fc_idx = cmd.index("-filter_complex")
    fc_val = cmd[fc_idx + 1]
    assert "[prev_v]" in fc_val


def test_hls_direct_main_output_uses_stream_segment():
    """Main recording output must use the stream_segment muxer."""
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
    formats = [cmd[i + 1] for i in f_indices]
    assert "stream_segment" in formats


def test_hls_direct_preview_output_uses_hls():
    """Preview output must use the hls muxer."""
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
    formats = [cmd[i + 1] for i in f_indices]
    assert "hls" in formats


def test_hls_direct_hls_time_emitted():
    """hls_direct command must include -hls_time with the configured value."""
    cfg = _base_config(rpo_kwargs={"hls_time": 3})
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-hls_time" in cmd
    idx = cmd.index("-hls_time")
    assert cmd[idx + 1] == "3"


def test_hls_direct_hls_list_size_emitted():
    """hls_direct command must include -hls_list_size with the configured value."""
    cfg = _base_config(rpo_kwargs={"hls_list_size": 7})
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-hls_list_size" in cmd
    idx = cmd.index("-hls_list_size")
    assert cmd[idx + 1] == "7"


def test_hls_direct_hls_flags_emitted():
    """hls_direct command must include -hls_flags with the configured flags."""
    flags = "delete_segments+append_list+independent_segments"
    cfg = _base_config(rpo_kwargs={"hls_flags": flags})
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-hls_flags" in cmd
    idx = cmd.index("-hls_flags")
    assert cmd[idx + 1] == flags


def test_hls_direct_segment_filename_ends_with_seg_pattern():
    """-hls_segment_filename must end with 'seg%05d.ts'."""
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-hls_segment_filename" in cmd
    idx = cmd.index("-hls_segment_filename")
    assert cmd[idx + 1].endswith("seg%05d.ts")


def test_hls_direct_playlist_ends_with_index_m3u8():
    """The last argument (HLS playlist path) must end with 'index.m3u8'."""
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert cmd[-1].endswith("index.m3u8")


def test_hls_direct_segment_filename_contains_channel_id():
    """-hls_segment_filename path must include the channel id."""
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    idx = cmd.index("-hls_segment_filename")
    assert "rts1" in cmd[idx + 1]


def test_hls_direct_playlist_contains_channel_id():
    """The HLS playlist path must include the channel id."""
    cfg = _base_config()
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "rts1" in cmd[-1]


def test_hls_direct_audio_enabled():
    """With audio_enabled=True, -c:a and -b:a must be present."""
    cfg = _base_config(rpo_kwargs={
        "audio_enabled": True,
        "audio_codec": "aac",
        "audio_bitrate": "96k",
        "audio_sample_rate": 48000,
    })
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-c:a" in cmd
    assert "aac" in cmd
    assert "-b:a" in cmd
    assert "-ar" in cmd


def test_hls_direct_audio_disabled():
    """With audio_enabled=False, -an must be present for the preview output."""
    cfg = _base_config(rpo_kwargs={"audio_enabled": False})
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-an" in cmd


def test_hls_direct_libx264_emits_gop_flags():
    """libx264 preview must emit -g, -keyint_min, -bf, -sc_threshold, -x264-params."""
    cfg = _base_config(rpo_kwargs={"video_codec": "libx264", "fps": 10})
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-g" in cmd
    assert "-keyint_min" in cmd
    assert "-bf" in cmd
    assert "-sc_threshold" in cmd
    assert "-x264-params" in cmd


def test_hls_direct_libx264_gop_value():
    """libx264 GOP must equal fps * 2."""
    cfg = _base_config(rpo_kwargs={"video_codec": "libx264", "fps": 10})
    cmd = _build_recording_command_with_hls_direct(cfg)
    g_idx = cmd.index("-g")
    assert cmd[g_idx + 1] == "20"  # fps=10 → gop=20


def test_hls_direct_nvenc_emits_forced_idr():
    """h264_nvenc preview must emit -forced-idr 1."""
    cfg = _base_config(rpo_kwargs={
        "video_codec": "h264_nvenc", "preset": "p1", "fail_safe_mode": False, "fps": 10
    })
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-forced-idr" in cmd
    assert cmd[cmd.index("-forced-idr") + 1] == "1"


def test_hls_direct_nvenc_emits_bf_zero():
    """h264_nvenc preview must emit -bf 0."""
    cfg = _base_config(rpo_kwargs={
        "video_codec": "h264_nvenc", "preset": "p1", "fail_safe_mode": False
    })
    cmd = _build_recording_command_with_hls_direct(cfg)
    assert "-bf" in cmd
    assert cmd[cmd.index("-bf") + 1] == "0"


def test_hls_direct_nvenc_fail_safe_logs_warning():
    """fail_safe_mode=True with h264_nvenc must emit a WARNING log but still build the command."""
    cfg = _base_config(rpo_kwargs={
        "video_codec": "h264_nvenc", "preset": "p1", "fail_safe_mode": True
    })
    with patch("app.services.ffmpeg_builder.logger") as mock_log:
        cmd = _build_recording_command_with_hls_direct(cfg)
    mock_log.warning.assert_called_once()
    # The format string (first arg) must mention NVENC
    assert "NVENC" in mock_log.warning.call_args[0][0]
    # Warning is informational — command must still include h264_nvenc codec
    cv_indices = [i for i, x in enumerate(cmd) if x == "-c:v"]
    codecs = [cmd[i + 1] for i in cv_indices]
    assert "h264_nvenc" in codecs


# ---------------------------------------------------------------------------
# HlsPreviewManager — _start_hls_direct
# ---------------------------------------------------------------------------

def test_start_hls_direct_registers_channel(tmp_path):
    """_start_hls_direct must add the channel to _hls_direct_channels."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    assert "rts1" in mgr._hls_direct_channels


def test_start_hls_direct_creates_output_dir(tmp_path):
    """_start_hls_direct must ensure the output directory exists."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    out_dir = tmp_path / "rts1"
    with patch.object(mgr, "_output_dir", return_value=out_dir):
        mgr._start_hls_direct("rts1", cfg)
    assert out_dir.exists()


def test_start_hls_direct_returns_none(tmp_path):
    """_start_hls_direct must return None (no process info)."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        result = mgr._start_hls_direct("rts1", cfg)
    assert result is None


def test_start_hls_direct_raises_when_rpo_none(tmp_path):
    """_start_hls_direct must raise RuntimeError when rpo is None."""
    cfg = ChannelConfig(
        id="rts1", name="RTS1", display_name="RTS1 Test",
        capture={"device_type": "dshow"},
        paths={"record_dir": "/tmp/rec", "chunks_dir": "/tmp/chunks", "final_dir": "/tmp/final"},
        preview=PreviewConfig(input_mode="hls_direct"),
    )
    mgr = HlsPreviewManager()
    with pytest.raises(RuntimeError, match="recording_preview_output"):
        mgr._start_hls_direct("rts1", cfg)


def test_start_hls_direct_raises_when_rpo_disabled(tmp_path):
    """_start_hls_direct must raise RuntimeError when rpo.enabled=False."""
    cfg = _base_config(rpo_enabled=False)
    mgr = HlsPreviewManager()
    with pytest.raises(RuntimeError, match="enabled=False"):
        mgr._start_hls_direct("rts1", cfg)


def test_start_hls_direct_raises_when_mode_not_hls_direct(tmp_path):
    """_start_hls_direct must raise RuntimeError when rpo.mode != 'hls_direct'."""
    cfg = _base_config(mode="udp", input_mode="from_udp")
    mgr = HlsPreviewManager()
    with pytest.raises(RuntimeError, match="hls_direct"):
        mgr._start_hls_direct("rts1", cfg)


# ---------------------------------------------------------------------------
# HlsPreviewManager — start_preview routing
# ---------------------------------------------------------------------------

def test_start_preview_hls_direct_calls_start_hls_direct(tmp_path):
    """start_preview with input_mode='hls_direct' must call _start_hls_direct."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_start_hls_direct", return_value=None) as mock_start:
        mgr.start_preview("rts1", cfg)
    mock_start.assert_called_once_with("rts1", cfg)


# ---------------------------------------------------------------------------
# HlsPreviewManager — is_running
# ---------------------------------------------------------------------------

def test_is_running_true_for_hls_direct_channel(tmp_path):
    """is_running must return True when the channel is registered as hls_direct."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    assert mgr.is_running("rts1") is True


def test_is_running_false_when_not_registered():
    """is_running must return False when the channel is not registered."""
    mgr = HlsPreviewManager()
    assert mgr.is_running("unknown") is False


# ---------------------------------------------------------------------------
# HlsPreviewManager — stop_preview
# ---------------------------------------------------------------------------

def test_stop_preview_returns_true_for_hls_direct(tmp_path):
    """stop_preview must return True when deregistering an hls_direct channel."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    result = mgr.stop_preview("rts1")
    assert result is True


def test_stop_preview_removes_hls_direct_channel(tmp_path):
    """stop_preview must remove the channel from _hls_direct_channels."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    mgr.stop_preview("rts1")
    assert "rts1" not in mgr._hls_direct_channels


def test_stop_preview_returns_false_when_not_running():
    """stop_preview must return False when the channel is not registered."""
    mgr = HlsPreviewManager()
    result = mgr.stop_preview("not_registered")
    assert result is False


def test_is_running_false_after_stop(tmp_path):
    """is_running must return False after stop_preview is called."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    mgr.stop_preview("rts1")
    assert mgr.is_running("rts1") is False


# ---------------------------------------------------------------------------
# HlsPreviewManager — preview_status
# ---------------------------------------------------------------------------

def test_preview_status_hls_direct_running_true(tmp_path):
    """preview_status must return running=True for an hls_direct channel."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    status = mgr.preview_status("rts1")
    assert status["running"] is True


def test_preview_status_hls_direct_pid_is_none(tmp_path):
    """preview_status must return pid=None for hls_direct (no separate process)."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    status = mgr.preview_status("rts1")
    assert status["pid"] is None


def test_preview_status_hls_direct_no_playlist_ready(tmp_path):
    """preview_status must return playlist_ready=False when m3u8 has no segments."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    status = mgr.preview_status("rts1")
    assert status["playlist_ready"] is False


def test_preview_status_hls_direct_startup_status_starting_when_no_playlist(tmp_path):
    """startup_status must be 'starting' while playlist is not yet ready."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    status = mgr.preview_status("rts1")
    assert status["startup_status"] == "starting"


def test_preview_status_hls_direct_playlist_ready_when_m3u8_ready(tmp_path):
    """playlist_ready=True when m3u8 contains at least one #EXTINF entry."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
        # Simulate the recording process having written a playlist
        playlist = tmp_path / "index.m3u8"
        playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nseg00001.ts\n", encoding="utf-8")
        status = mgr.preview_status("rts1")
    assert status["playlist_ready"] is True


def test_preview_status_hls_direct_startup_status_running_when_playlist_ready(tmp_path):
    """startup_status='running' when playlist has at least one segment."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
        playlist = tmp_path / "index.m3u8"
        playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nseg00001.ts\n", encoding="utf-8")
        status = mgr.preview_status("rts1")
    assert status["startup_status"] == "running"


def test_preview_status_hls_direct_has_playlist_url(tmp_path):
    """preview_status must include a non-None playlist_url for hls_direct."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    status = mgr.preview_status("rts1")
    assert status["playlist_url"] is not None
    assert "rts1" in status["playlist_url"]


def test_preview_status_hls_direct_no_stderr_tail(tmp_path):
    """hls_direct status must return empty stderr_tail (no separate process)."""
    cfg = _base_config()
    mgr = HlsPreviewManager()
    with patch.object(mgr, "_output_dir", return_value=tmp_path):
        mgr._start_hls_direct("rts1", cfg)
    status = mgr.preview_status("rts1")
    assert status["stderr_tail"] == []


# ---------------------------------------------------------------------------
# rts1.json — Phase 22 sanity
# ---------------------------------------------------------------------------

def test_rts1_rpo_mode_is_hls_direct():
    """rts1.json must have recording_preview_output.mode='hls_direct'."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output is not None
    assert cfg.recording_preview_output.mode == "hls_direct"


def test_rts1_rpo_hls_time():
    """rts1.json must have hls_time=2."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output.hls_time == 2


def test_rts1_rpo_hls_list_size():
    """rts1.json must have hls_list_size=5."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output.hls_list_size == 5


def test_rts1_rpo_hls_flags_has_delete_segments():
    cfg = _load_rts1()
    assert "delete_segments" in cfg.recording_preview_output.hls_flags


def test_rts1_rpo_hls_flags_has_independent_segments():
    cfg = _load_rts1()
    assert "independent_segments" in cfg.recording_preview_output.hls_flags


def test_rts1_preview_input_mode_hls_direct():
    """rts1.json must have preview.input_mode='hls_direct'."""
    cfg = _load_rts1()
    assert cfg.preview.input_mode == "hls_direct"


def test_rts1_build_produces_hls_format():
    """Building rts1 command must produce -f hls for the preview output."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
    formats = [cmd[i + 1] for i in f_indices]
    assert "hls" in formats


def test_rts1_build_produces_hls_segment_filename():
    """Building rts1 command must include -hls_segment_filename."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-hls_segment_filename" in cmd
