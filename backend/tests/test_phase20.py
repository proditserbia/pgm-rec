"""
Phase 20 unit tests — Switch rts1.json from h264_nvenc to libx264 preview.

Covers libx264 preview builder changes:
- tune is now emitted for libx264 (previously blocked, was h264_nvenc-only)
- emits -g fps*2
- emits -keyint_min fps*2
- emits -sc_threshold 0
- -x264-params contains keyint=fps*2 (not fps as in Phase 19)
- -g / -keyint_min / -sc_threshold appear before -b:v
- -g value tracks rpo.fps

Covers rts1.json sanity after Phase 20 switch:
- video_codec is libx264
- preset is ultrafast
- tune is zerolatency
- fallback_to_cpu is False
- command has -tune zerolatency
- command has -g 20 (fps=10, gop=fps*2)
- command has -keyint_min 20
- command has -sc_threshold 0
- command has -x264-params with keyint=20
"""
from __future__ import annotations

from pathlib import Path

from app.models.schemas import ChannelConfig, RecordingPreviewOutputConfig
from app.services.ffmpeg_builder import build_ffmpeg_command


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg_libx264(fps: int = 10, **rpo_kwargs) -> ChannelConfig:
    rpo_kwargs.setdefault("enabled", True)
    rpo_kwargs.setdefault("video_codec", "libx264")
    rpo_kwargs.setdefault("fail_safe_mode", False)
    rpo_kwargs.setdefault("fps", fps)
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
        recording_preview_output=RecordingPreviewOutputConfig(**rpo_kwargs),
    )


def _load_rts1() -> ChannelConfig:
    base = Path(__file__).parent.parent / "data" / "channels"
    return ChannelConfig.model_validate_json((base / "rts1.json").read_text())


# ---------------------------------------------------------------------------
# libx264 — tune now emitted
# ---------------------------------------------------------------------------

def test_libx264_preview_emits_tune_when_set():
    """-tune must be emitted for libx264 preview when tune is configured."""
    cmd = build_ffmpeg_command(_cfg_libx264(tune="zerolatency"))
    assert "-tune" in cmd
    assert cmd[cmd.index("-tune") + 1] == "zerolatency"


def test_libx264_preview_no_tune_when_not_set():
    """-tune must NOT be emitted for libx264 when tune is None."""
    cmd = build_ffmpeg_command(_cfg_libx264(tune=None))
    assert "-tune" not in cmd


# ---------------------------------------------------------------------------
# libx264 — GOP options (fps * 2)
# ---------------------------------------------------------------------------

def test_libx264_preview_g_equals_fps_times_two():
    """-g must equal rpo.fps * 2 for libx264 preview."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    assert "-g" in cmd
    assert cmd[cmd.index("-g") + 1] == "20"


def test_libx264_preview_g_tracks_fps():
    """-g must equal fps*2 for a non-default fps."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=25))
    assert "-g" in cmd
    assert cmd[cmd.index("-g") + 1] == "50"


def test_libx264_preview_keyint_min_equals_fps_times_two():
    """-keyint_min must equal rpo.fps * 2 for libx264 preview."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    assert "-keyint_min" in cmd
    assert cmd[cmd.index("-keyint_min") + 1] == "20"


def test_libx264_preview_keyint_min_tracks_fps():
    """-keyint_min must equal fps*2 for a non-default fps."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=25))
    assert "-keyint_min" in cmd
    assert cmd[cmd.index("-keyint_min") + 1] == "50"


def test_libx264_preview_sc_threshold_zero():
    """-sc_threshold 0 must be emitted for libx264 preview output."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-sc_threshold" in cmd
    assert cmd[cmd.index("-sc_threshold") + 1] == "0"


def test_libx264_preview_x264_params_keyint_equals_fps_times_two():
    """-x264-params keyint must equal fps*2."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "keyint=20" in params


def test_libx264_preview_x264_params_no_min_keyint():
    """-x264-params must NOT contain min-keyint (now a separate -keyint_min flag)."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "min-keyint" not in params


def test_libx264_preview_x264_params_no_scenecut():
    """-x264-params must NOT contain scenecut (now a separate -sc_threshold flag)."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "scenecut" not in params


def test_libx264_preview_gop_options_before_bitrate():
    """-g, -keyint_min, -sc_threshold must appear before -b:v in the preview section."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    idx_g = cmd.index("-g")
    idx_km = cmd.index("-keyint_min")
    idx_sc = cmd.index("-sc_threshold")
    bv_indices = [i for i, x in enumerate(cmd) if x == "-b:v"]
    last_bv = bv_indices[-1]
    assert idx_g < last_bv
    assert idx_km < last_bv
    assert idx_sc < last_bv


# ---------------------------------------------------------------------------
# rts1.json sanity — Phase 20 switch to libx264
# ---------------------------------------------------------------------------

def test_rts1_codec_is_libx264():
    """rts1.json must use libx264 after Phase 20 switch."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output is not None
    assert cfg.recording_preview_output.video_codec == "libx264"


def test_rts1_preset_is_ultrafast():
    """rts1.json preset must be 'ultrafast'."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output.preset == "ultrafast"


def test_rts1_tune_is_zerolatency():
    """rts1.json tune must be 'zerolatency'."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output.tune == "zerolatency"


def test_rts1_fallback_to_cpu_is_false():
    """rts1.json fallback_to_cpu must be False after Phase 20 switch."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output.fallback_to_cpu is False


def test_rts1_command_has_tune_zerolatency():
    """Full rts1 command must include -tune zerolatency."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-tune" in cmd
    assert cmd[cmd.index("-tune") + 1] == "zerolatency"


def test_rts1_command_has_g_twenty():
    """Full rts1 command must include -g 20 (fps=10, gop=fps*2)."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output.fps == 10
    cmd = build_ffmpeg_command(cfg)
    assert "-g" in cmd
    assert cmd[cmd.index("-g") + 1] == "20"


def test_rts1_command_has_keyint_min_twenty():
    """Full rts1 command must include -keyint_min 20."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-keyint_min" in cmd
    assert cmd[cmd.index("-keyint_min") + 1] == "20"


def test_rts1_command_has_sc_threshold_zero():
    """Full rts1 command must include -sc_threshold 0."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-sc_threshold" in cmd
    assert cmd[cmd.index("-sc_threshold") + 1] == "0"


def test_rts1_command_has_x264_params_with_keyint20():
    """Full rts1 command must include -x264-params containing keyint=20."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-x264-params" in cmd
    params = cmd[cmd.index("-x264-params") + 1]
    assert "repeat-headers=1" in params
    assert "keyint=20" in params
