"""
Phase 12 unit tests — UDP/HLS Preview with Audio and NVENC Support.

Covers:
- RecordingPreviewOutputConfig: schema defaults and field validation
- PreviewConfig: from_udp input_mode and fallback_to_cpu field
- ChannelConfig: recording_preview_output field default and usage
- _build_filter_complex_with_preview: correct filter_complex string
- build_ffmpeg_command with recording_preview_output disabled: unchanged (-vf path)
- build_ffmpeg_command with recording_preview_output enabled: dual output
  - filter_complex present with [main_v] and [prev_v]
  - main output: stream_segment, mapped, correct codec
  - preview output: UDP URL, mapped, NVENC codec, preset, tune
  - preview audio enabled: -map 0:a, -c:a, -b:a, -ar emitted
  - preview audio disabled: -an emitted, no -map 0:a for preview
  - fail_safe_mode + NVENC triggers logger.warning
  - tune only emitted when video_codec == h264_nvenc
  - libx264 preset (no tune)
- build_hls_preview_from_udp_command: command structure
  - -fflags +nobuffer+genpts
  - input URL from recording_preview_output.url
  - -c:v copy
  - -c:a copy when audio_enabled=True
  - -an when audio_enabled=False
  - HLS muxer flags present
  - raises ValueError when recording_preview_output is None
- build_hls_preview_command raises ValueError for from_udp input_mode
- HlsPreviewManager.start_preview with from_udp mode
  - launches _start_from_udp
  - raises RuntimeError when recording_preview_output is None
  - raises RuntimeError when recording_preview_output.enabled=False
- rts1.json Phase 12 update: recording_preview_output fields, preview.input_mode
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
    _build_filter_complex_with_preview,
    build_ffmpeg_command,
    build_hls_preview_command,
    build_hls_preview_from_udp_command,
)
from app.services.hls_preview_manager import HlsPreviewManager
from app.services.process_manager import ProcessManager, _is_nvenc_failure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_config(
    *,
    rpo_enabled: bool = False,
    rpo_kwargs: dict | None = None,
    device_type: str = "dshow",
) -> ChannelConfig:
    """Create a minimal ChannelConfig, optionally with recording_preview_output."""
    rpo = None
    if rpo_kwargs is not None or rpo_enabled:
        rpo_kwargs = rpo_kwargs or {}
        rpo_kwargs.setdefault("enabled", rpo_enabled)
        rpo = RecordingPreviewOutputConfig(**rpo_kwargs)

    return ChannelConfig(
        id="rts1",
        name="RTS1",
        display_name="RTS1 Test",
        capture={"device_type": device_type},
        paths={
            "record_dir": "/tmp/rec",
            "chunks_dir": "/tmp/chunks",
            "final_dir": "/tmp/final",
        },
        recording_preview_output=rpo,
    )


def _load_rts1() -> ChannelConfig:
    base = Path(__file__).parent.parent / "data" / "channels"
    return ChannelConfig.model_validate_json((base / "rts1.json").read_text())


def _mock_process(returncode=None):
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 55555
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


# ---------------------------------------------------------------------------
# RecordingPreviewOutputConfig schema
# ---------------------------------------------------------------------------

def test_recording_preview_output_defaults():
    rpo = RecordingPreviewOutputConfig()
    assert rpo.enabled is False
    assert rpo.url == "udp://127.0.0.1:23001?pkt_size=1316"
    assert rpo.format == "mpegts"
    assert rpo.video_codec == "libx264"
    assert rpo.preset == "veryfast"
    assert rpo.tune is None
    assert rpo.width == 480
    assert rpo.height == 270
    assert rpo.fps == 10
    assert rpo.bitrate == "400k"
    assert rpo.audio_enabled is False
    assert rpo.audio_codec == "aac"
    assert rpo.audio_bitrate == "96k"
    assert rpo.audio_sample_rate == 48000
    assert rpo.fail_safe_mode is True
    assert rpo.fallback_to_cpu is False


def test_recording_preview_output_nvenc_config():
    rpo = RecordingPreviewOutputConfig(
        enabled=True,
        video_codec="h264_nvenc",
        preset="p1",
        tune="ull",
        audio_enabled=True,
        fail_safe_mode=True,
        fallback_to_cpu=True,
    )
    assert rpo.video_codec == "h264_nvenc"
    assert rpo.preset == "p1"
    assert rpo.tune == "ull"
    assert rpo.audio_enabled is True
    assert rpo.fallback_to_cpu is True


def test_recording_preview_output_no_tune_for_libx264():
    rpo = RecordingPreviewOutputConfig(
        video_codec="libx264",
        tune=None,
    )
    assert rpo.tune is None


def test_recording_preview_output_custom_url():
    rpo = RecordingPreviewOutputConfig(url="udp://192.168.1.100:23001?pkt_size=1316")
    assert "192.168.1.100" in rpo.url


# ---------------------------------------------------------------------------
# PreviewConfig Phase 12 additions
# ---------------------------------------------------------------------------

def test_preview_config_from_udp_input_mode():
    p = PreviewConfig(input_mode="from_udp")
    assert p.input_mode == "from_udp"


def test_preview_config_fallback_to_cpu_default():
    p = PreviewConfig()
    assert p.fallback_to_cpu is False


def test_preview_config_fallback_to_cpu_set():
    p = PreviewConfig(fallback_to_cpu=True)
    assert p.fallback_to_cpu is True


# ---------------------------------------------------------------------------
# ChannelConfig.recording_preview_output field
# ---------------------------------------------------------------------------

def test_channel_config_recording_preview_output_default_none():
    cfg = _base_config()
    assert cfg.recording_preview_output is None


def test_channel_config_recording_preview_output_set():
    cfg = _base_config(rpo_enabled=True)
    assert cfg.recording_preview_output is not None
    assert cfg.recording_preview_output.enabled is True


def test_channel_config_round_trip_with_rpo():
    cfg = _base_config(
        rpo_enabled=True,
        rpo_kwargs={
            "enabled": True,
            "video_codec": "h264_nvenc",
            "audio_enabled": True,
        },
    )
    data = cfg.model_dump_json()
    cfg2 = ChannelConfig.model_validate_json(data)
    assert cfg2.recording_preview_output is not None
    assert cfg2.recording_preview_output.video_codec == "h264_nvenc"
    assert cfg2.recording_preview_output.audio_enabled is True


# ---------------------------------------------------------------------------
# _build_filter_complex_with_preview
# ---------------------------------------------------------------------------

def test_filter_complex_contains_split():
    cfg = _base_config(rpo_enabled=True)
    fc = _build_filter_complex_with_preview(cfg)
    assert "split=2" in fc
    assert "[raw_m]" in fc
    assert "[raw_p]" in fc


def test_filter_complex_main_v_pad():
    cfg = _base_config(rpo_enabled=True)
    fc = _build_filter_complex_with_preview(cfg)
    assert "[main_v]" in fc


def test_filter_complex_prev_v_pad():
    cfg = _base_config(rpo_enabled=True)
    fc = _build_filter_complex_with_preview(cfg)
    assert "[prev_v]" in fc


def test_filter_complex_main_chain_scale():
    cfg = _base_config(rpo_enabled=True)
    cfg.filters.scale_width = 1024
    cfg.filters.scale_height = 576
    fc = _build_filter_complex_with_preview(cfg)
    assert "scale=1024:576" in fc


def test_filter_complex_main_chain_yadif_when_deinterlace_enabled():
    cfg = _base_config(rpo_enabled=True)
    cfg.filters.deinterlace = True
    fc = _build_filter_complex_with_preview(cfg)
    assert "yadif" in fc


def test_filter_complex_no_yadif_when_deinterlace_disabled():
    cfg = _base_config(rpo_enabled=True)
    cfg.filters.deinterlace = False
    fc = _build_filter_complex_with_preview(cfg)
    assert "yadif" not in fc


def test_filter_complex_prev_chain_scale_fps():
    rpo = RecordingPreviewOutputConfig(enabled=True, width=480, height=270, fps=10)
    cfg = _base_config(rpo_enabled=True)
    cfg.recording_preview_output = rpo
    fc = _build_filter_complex_with_preview(cfg)
    assert "scale=480:270" in fc
    assert "fps=10" in fc


def test_filter_complex_no_overlay_when_disabled():
    cfg = _base_config(rpo_enabled=True)
    cfg.filters.overlay.enabled = False
    fc = _build_filter_complex_with_preview(cfg)
    assert "drawtext" not in fc


# ---------------------------------------------------------------------------
# build_ffmpeg_command — single output (preview disabled)
# ---------------------------------------------------------------------------

def test_build_ffmpeg_command_single_output_uses_vf_not_filter_complex():
    """When recording_preview_output is None or disabled, use simple -vf path."""
    cfg = _base_config()
    cmd = build_ffmpeg_command(cfg)
    assert "-vf" in cmd
    assert "-filter_complex" not in cmd


def test_build_ffmpeg_command_disabled_rpo_uses_vf():
    cfg = _base_config(rpo_kwargs={"enabled": False})
    cmd = build_ffmpeg_command(cfg)
    assert "-vf" in cmd
    assert "-filter_complex" not in cmd


# ---------------------------------------------------------------------------
# build_ffmpeg_command — dual output (preview enabled)
# ---------------------------------------------------------------------------

def test_build_ffmpeg_command_with_preview_uses_filter_complex():
    cfg = _base_config(rpo_enabled=True)
    cmd = build_ffmpeg_command(cfg)
    assert "-filter_complex" in cmd
    assert "-vf" not in cmd


def test_build_ffmpeg_command_with_preview_maps_main_v():
    cfg = _base_config(rpo_enabled=True)
    cmd = build_ffmpeg_command(cfg)
    assert "-map" in cmd
    assert "[main_v]" in cmd


def test_build_ffmpeg_command_with_preview_maps_prev_v():
    cfg = _base_config(rpo_enabled=True)
    cmd = build_ffmpeg_command(cfg)
    assert "[prev_v]" in cmd


def test_build_ffmpeg_command_with_preview_main_audio_mapped():
    """Main recording output must include audio (0:a)."""
    cfg = _base_config(rpo_enabled=True)
    cmd = build_ffmpeg_command(cfg)
    map_indices = [i for i, x in enumerate(cmd) if x == "-map"]
    mapped_values = [cmd[i + 1] for i in map_indices]
    assert "0:a" in mapped_values


def test_build_ffmpeg_command_with_preview_main_codec():
    cfg = _base_config(rpo_enabled=True)
    cmd = build_ffmpeg_command(cfg)
    # Should have -c:v libx264 (main encoding codec)
    cv_indices = [i for i, x in enumerate(cmd) if x == "-c:v"]
    codecs = [cmd[i + 1] for i in cv_indices]
    assert "libx264" in codecs


def test_build_ffmpeg_command_with_preview_udp_url_present():
    cfg = _base_config(
        rpo_kwargs={"enabled": True, "url": "udp://127.0.0.1:23001?pkt_size=1316"}
    )
    cmd = build_ffmpeg_command(cfg)
    assert "udp://127.0.0.1:23001?pkt_size=1316" in cmd


def test_build_ffmpeg_command_with_preview_mpegts_format():
    cfg = _base_config(rpo_enabled=True)
    cmd = build_ffmpeg_command(cfg)
    # Last -f before the UDP URL should be 'mpegts'
    f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
    formats = [cmd[i + 1] for i in f_indices]
    assert "mpegts" in formats


def test_build_ffmpeg_command_with_preview_nvenc_codec():
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "h264_nvenc",
            "preset": "p1",
            "fail_safe_mode": False,
        }
    )
    cmd = build_ffmpeg_command(cfg)
    cv_indices = [i for i, x in enumerate(cmd) if x == "-c:v"]
    codecs = [cmd[i + 1] for i in cv_indices]
    assert "h264_nvenc" in codecs


def test_build_ffmpeg_command_with_preview_nvenc_tune():
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "h264_nvenc",
            "preset": "p1",
            "tune": "ull",
            "fail_safe_mode": False,
        }
    )
    cmd = build_ffmpeg_command(cfg)
    assert "-tune" in cmd
    assert cmd[cmd.index("-tune") + 1] == "ull"


def test_build_ffmpeg_command_with_preview_no_tune_for_libx264():
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "libx264",
            "tune": "ull",  # tune set but codec is libx264 — must not be emitted
        }
    )
    cmd = build_ffmpeg_command(cfg)
    assert "-tune" not in cmd


def test_build_ffmpeg_command_with_preview_audio_enabled():
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "audio_enabled": True,
            "audio_codec": "aac",
            "audio_bitrate": "96k",
            "audio_sample_rate": 48000,
        }
    )
    cmd = build_ffmpeg_command(cfg)
    assert "-c:a" in cmd
    assert "aac" in cmd
    assert "-ar" in cmd
    assert "48000" in cmd
    assert "-an" not in cmd


def test_build_ffmpeg_command_with_preview_audio_disabled():
    cfg = _base_config(rpo_kwargs={"enabled": True, "audio_enabled": False})
    cmd = build_ffmpeg_command(cfg)
    # -an must appear (after the -map "[prev_v]") for preview output
    assert "-an" in cmd


def test_build_ffmpeg_command_with_preview_fail_safe_warning(caplog):
    """fail_safe_mode=True + h264_nvenc must log a WARNING."""
    import logging
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "h264_nvenc",
            "fail_safe_mode": True,
        }
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ffmpeg_builder"):
        build_ffmpeg_command(cfg)
    assert any("NVENC" in r.message and "fail_safe_mode" in r.message for r in caplog.records)


def test_build_ffmpeg_command_with_preview_no_warning_for_libx264(caplog):
    """No warning when using libx264 regardless of fail_safe_mode."""
    import logging
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "libx264",
            "fail_safe_mode": True,
        }
    )
    with caplog.at_level(logging.WARNING, logger="app.services.ffmpeg_builder"):
        build_ffmpeg_command(cfg)
    nvenc_warnings = [r for r in caplog.records if "NVENC" in r.message]
    assert not nvenc_warnings


def test_build_ffmpeg_command_with_preview_segment_muxer_present():
    """Main recording output must still use stream_segment muxer."""
    cfg = _base_config(rpo_enabled=True)
    cmd = build_ffmpeg_command(cfg)
    assert "stream_segment" in cmd


def test_build_ffmpeg_command_with_preview_bitrate():
    cfg = _base_config(rpo_kwargs={"enabled": True, "bitrate": "600k"})
    cmd = build_ffmpeg_command(cfg)
    assert "600k" in cmd


# ---------------------------------------------------------------------------
# build_hls_preview_from_udp_command
# ---------------------------------------------------------------------------

def test_build_hls_preview_from_udp_command_structure(tmp_path):
    cfg = _base_config(rpo_kwargs={"enabled": True, "url": "udp://127.0.0.1:23001?pkt_size=1316"})
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)

    assert cmd[0] == cfg.ffmpeg_path
    assert "-y" in cmd
    assert "-fflags" in cmd
    fflags = cmd[cmd.index("-fflags") + 1]
    assert "nobuffer" in fflags
    assert "genpts" in fflags
    assert "-i" in cmd
    assert "udp://127.0.0.1:23001?pkt_size=1316" in cmd
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_build_hls_preview_from_udp_audio_copy_when_enabled(tmp_path):
    cfg = _base_config(rpo_kwargs={"enabled": True, "audio_enabled": True})
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    assert "-c:a" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "-an" not in cmd


def test_build_hls_preview_from_udp_no_audio_when_disabled(tmp_path):
    cfg = _base_config(rpo_kwargs={"enabled": True, "audio_enabled": False})
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    assert "-an" in cmd
    assert "-c:a" not in cmd


def test_build_hls_preview_from_udp_hls_muxer(tmp_path):
    cfg = _base_config(rpo_enabled=True)
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    assert "-f" in cmd
    f_idx = [i for i, x in enumerate(cmd) if x == "-f"]
    assert any(cmd[i + 1] == "hls" for i in f_idx)
    assert "-hls_time" in cmd
    assert "-hls_list_size" in cmd
    assert "-hls_flags" in cmd
    assert "-hls_segment_filename" in cmd
    assert str(tmp_path / "index.m3u8") == cmd[-1]


def test_build_hls_preview_from_udp_raises_without_rpo(tmp_path):
    cfg = _base_config()  # no recording_preview_output
    with pytest.raises(ValueError, match="recording_preview_output"):
        build_hls_preview_from_udp_command(cfg, tmp_path)


def test_build_hls_preview_command_raises_for_from_udp(tmp_path):
    """build_hls_preview_command must raise ValueError for from_udp mode."""
    cfg = _base_config()
    cfg.preview.input_mode = "from_udp"
    with pytest.raises(ValueError, match="from_udp"):
        build_hls_preview_command(cfg, tmp_path)


# ---------------------------------------------------------------------------
# HlsPreviewManager — from_udp mode
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    return HlsPreviewManager()


def test_manager_start_preview_from_udp(manager, tmp_path):
    cfg = _base_config(
        rpo_kwargs={"enabled": True, "url": "udp://127.0.0.1:23001?pkt_size=1316"},
    )
    cfg.preview.input_mode = "from_udp"
    mock_proc = _mock_process()

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings, \
         patch("subprocess.Popen", return_value=mock_proc):
        ms = MagicMock()
        ms.logs_dir = tmp_path / "logs"
        ms.preview_dir = tmp_path / "preview"
        ms.logs_dir.mkdir(parents=True, exist_ok=True)
        ms.preview_dir.mkdir(parents=True, exist_ok=True)
        mock_settings.return_value = ms

        info = manager.start_preview("rts1", cfg)

    assert info is not None
    assert info.pid == 55555
    assert info.input_mode == "from_udp"
    assert manager.is_running("rts1")


def test_manager_start_preview_from_udp_no_rpo_raises(manager):
    """from_udp mode without recording_preview_output configured → RuntimeError."""
    cfg = _base_config()  # no rpo
    cfg.preview.input_mode = "from_udp"
    with pytest.raises(RuntimeError, match="recording_preview_output"):
        manager.start_preview("rts1", cfg)


def test_manager_start_preview_from_udp_rpo_disabled_raises(manager):
    """from_udp mode with recording_preview_output.enabled=False → RuntimeError."""
    cfg = _base_config(rpo_kwargs={"enabled": False})
    cfg.preview.input_mode = "from_udp"
    with pytest.raises(RuntimeError, match="recording_preview_output"):
        manager.start_preview("rts1", cfg)


# ---------------------------------------------------------------------------
# rts1.json Phase 12 update
# ---------------------------------------------------------------------------

def test_rts1_has_recording_preview_output():
    cfg = _load_rts1()
    assert cfg.recording_preview_output is not None


def test_rts1_recording_preview_output_enabled():
    cfg = _load_rts1()
    assert cfg.recording_preview_output.enabled is True


def test_rts1_recording_preview_output_nvenc():
    cfg = _load_rts1()
    assert cfg.recording_preview_output.video_codec == "h264_nvenc"


def test_rts1_recording_preview_output_audio():
    cfg = _load_rts1()
    rpo = cfg.recording_preview_output
    assert rpo.audio_enabled is True
    assert rpo.audio_codec == "aac"
    assert rpo.audio_bitrate == "96k"
    assert rpo.audio_sample_rate == 48000


def test_rts1_recording_preview_output_fail_safe():
    cfg = _load_rts1()
    assert cfg.recording_preview_output.fail_safe_mode is True


def test_rts1_preview_input_mode_from_udp():
    cfg = _load_rts1()
    assert cfg.preview.input_mode == "from_udp"


def test_rts1_round_trip_with_recording_preview_output():
    cfg = _load_rts1()
    data = cfg.model_dump_json()
    cfg2 = ChannelConfig.model_validate_json(data)
    assert cfg2.recording_preview_output is not None
    assert cfg2.recording_preview_output.video_codec == cfg.recording_preview_output.video_codec
    assert cfg2.preview.input_mode == "from_udp"


# ---------------------------------------------------------------------------
# build_hls_preview_from_udp_command — codec validation (Phase 12 sanity)
# ---------------------------------------------------------------------------

def test_build_hls_preview_from_udp_raises_for_non_h264_video_codec(tmp_path):
    """Non-H.264 video codec (e.g. mpeg4) must raise ValueError with clear message."""
    cfg = _base_config(rpo_kwargs={"enabled": True, "video_codec": "mpeg4"})
    with pytest.raises(ValueError, match="H.264"):
        build_hls_preview_from_udp_command(cfg, tmp_path)


def test_build_hls_preview_from_udp_raises_for_hevc_video_codec(tmp_path):
    """HEVC/libx265 codec must also raise ValueError (not H.264)."""
    cfg = _base_config(rpo_kwargs={"enabled": True, "video_codec": "libx265"})
    with pytest.raises(ValueError, match="H.264"):
        build_hls_preview_from_udp_command(cfg, tmp_path)


def test_build_hls_preview_from_udp_raises_for_non_aac_audio(tmp_path):
    """Non-AAC audio codec (e.g. mp3) must raise ValueError when audio enabled."""
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "libx264",
            "audio_enabled": True,
            "audio_codec": "libmp3lame",
        }
    )
    with pytest.raises(ValueError, match="AAC"):
        build_hls_preview_from_udp_command(cfg, tmp_path)


def test_build_hls_preview_from_udp_no_audio_codec_check_when_disabled(tmp_path):
    """Non-AAC audio codec must NOT raise when audio_enabled=False."""
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "libx264",
            "audio_enabled": False,
            "audio_codec": "libmp3lame",
        }
    )
    # Should not raise
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    assert "-an" in cmd


def test_build_hls_preview_from_udp_nvenc_codec_accepted(tmp_path):
    """h264_nvenc is H.264-compatible and must not raise ValueError."""
    cfg = _base_config(
        rpo_kwargs={"enabled": True, "video_codec": "h264_nvenc", "audio_enabled": False}
    )
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    assert cmd[0] == cfg.ffmpeg_path


# ---------------------------------------------------------------------------
# ProcessManager — NVENC fallback (Phase 12 sanity)
# ---------------------------------------------------------------------------


def _make_db_mock():
    """Return a minimal SQLAlchemy session mock that accepts add/commit/query."""
    db = MagicMock()
    # query(...).filter(...).order_by(...).first() chain → None (no existing record)
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    return db


def test_process_manager_nvenc_fallback_retries_with_libx264(tmp_path):
    """
    When fallback_to_cpu=True and FFmpeg exits immediately with NVENC keywords
    in stderr, start() must retry once with video_codec='libx264'.
    """
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "h264_nvenc",
            "fallback_to_cpu": True,
        }
    )

    # First process exits immediately; second stays alive.
    nvenc_proc = _mock_process(returncode=1)
    cpu_proc = _mock_process(returncode=None)

    db = _make_db_mock()
    pm = ProcessManager()

    with (
        patch("app.services.process_manager.subprocess.Popen", side_effect=[nvenc_proc, cpu_proc]),
        patch("app.services.process_manager._is_nvenc_failure", return_value=True),
        patch("app.services.process_manager.time.sleep"),
        patch("app.services.process_manager.get_settings") as mock_settings,
        patch("app.services.process_manager.resolve_channel_path", return_value=tmp_path),
        patch("app.services.process_manager.shutil.disk_usage") as mock_du,
    ):
        ms = MagicMock()
        ms.logs_dir = tmp_path / "logs"
        ms.logs_dir.mkdir(parents=True, exist_ok=True)
        ms.min_free_disk_bytes = 0
        ms.log_max_files_per_channel = 10
        ms.restart_pre_delay_seconds = 0
        mock_settings.return_value = ms
        mock_du.return_value = MagicMock(free=10 * 1024 * 1024 * 1024)

        info = pm.start("rts1", cfg, db)

    # The returned info must belong to the CPU-fallback process.
    assert info.pid == cpu_proc.pid
    assert pm.is_running("rts1")


def test_process_manager_nvenc_no_fallback_when_not_configured(tmp_path):
    """
    When fallback_to_cpu=False, start() must NOT retry even if FFmpeg exits
    immediately — it simply returns the ProcessInfo as normal.
    """
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "h264_nvenc",
            "fallback_to_cpu": False,
        }
    )

    # Process stays alive (no crash).
    proc = _mock_process(returncode=None)

    db = _make_db_mock()
    pm = ProcessManager()

    with (
        patch("app.services.process_manager.subprocess.Popen", return_value=proc),
        patch("app.services.process_manager.time.sleep"),
        patch("app.services.process_manager.get_settings") as mock_settings,
        patch("app.services.process_manager.resolve_channel_path", return_value=tmp_path),
        patch("app.services.process_manager.shutil.disk_usage") as mock_du,
    ):
        ms = MagicMock()
        ms.logs_dir = tmp_path / "logs"
        ms.logs_dir.mkdir(parents=True, exist_ok=True)
        ms.min_free_disk_bytes = 0
        ms.log_max_files_per_channel = 10
        mock_settings.return_value = ms
        mock_du.return_value = MagicMock(free=10 * 1024 * 1024 * 1024)

        info = pm.start("rts1", cfg, db)

    assert info.pid == proc.pid


def test_process_manager_nvenc_fallback_not_triggered_for_libx264(tmp_path):
    """
    When video_codec is already 'libx264', the fallback block must not execute
    (no extra sleep or retry).
    """
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "video_codec": "libx264",
            "fallback_to_cpu": True,  # True but codec already CPU
        }
    )

    proc = _mock_process(returncode=None)
    db = _make_db_mock()
    pm = ProcessManager()

    with (
        patch("app.services.process_manager.subprocess.Popen", return_value=proc),
        patch("app.services.process_manager.time.sleep") as mock_sleep,
        patch("app.services.process_manager.get_settings") as mock_settings,
        patch("app.services.process_manager.resolve_channel_path", return_value=tmp_path),
        patch("app.services.process_manager.shutil.disk_usage") as mock_du,
    ):
        ms = MagicMock()
        ms.logs_dir = tmp_path / "logs"
        ms.logs_dir.mkdir(parents=True, exist_ok=True)
        ms.min_free_disk_bytes = 0
        ms.log_max_files_per_channel = 10
        mock_settings.return_value = ms
        mock_du.return_value = MagicMock(free=10 * 1024 * 1024 * 1024)

        info = pm.start("rts1", cfg, db)

    # _NVENC_CRASH_WAIT sleep must NOT have been called.
    mock_sleep.assert_not_called()
    assert info.pid == proc.pid


# ---------------------------------------------------------------------------
# _is_nvenc_failure helper
# ---------------------------------------------------------------------------

def test_is_nvenc_failure_detects_nvenc_keyword(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("Error initializing output stream: Error while opening encoder\n"
                   "NVENC Error: No NVENC capable devices found\n")
    assert _is_nvenc_failure(log) is True


def test_is_nvenc_failure_detects_nvcuda_keyword(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("Cannot load nvcuda.dll\n")
    assert _is_nvenc_failure(log) is True


def test_is_nvenc_failure_returns_false_for_unrelated_error(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("Invalid data found when processing input\n")
    assert _is_nvenc_failure(log) is False


def test_is_nvenc_failure_returns_false_for_missing_file(tmp_path):
    log = tmp_path / "nonexistent.log"
    assert _is_nvenc_failure(log) is False
