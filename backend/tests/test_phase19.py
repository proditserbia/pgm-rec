"""
Phase 19 unit tests — Reliable HLS index.m3u8 via improved UDP preview options.

Covers h264_nvenc preview output:
- emits -bf 0
- emits -rc cbr
- emits -repeat_headers 1
- these appear after -g and before -b:v

Covers libx264 preview output:
- emits -x264-params with repeat-headers=1, keyint=fps, min-keyint=fps, scenecut=0
- keyint/min-keyint track rpo.fps
- emits -bf 0

Covers both codecs:
- emits -muxdelay 0
- emits -muxpreload 0
- -muxdelay / -muxpreload appear after -f mpegts and before the UDP URL

Covers rts1.json sanity (h264_nvenc path).
"""
from __future__ import annotations

from pathlib import Path

from app.models.schemas import ChannelConfig, RecordingPreviewOutputConfig
from app.services.ffmpeg_builder import build_ffmpeg_command


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg_nvenc(fps: int = 10, **rpo_kwargs) -> ChannelConfig:
    rpo_kwargs.setdefault("enabled", True)
    rpo_kwargs.setdefault("video_codec", "h264_nvenc")
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
# h264_nvenc — new Phase 19 options
# ---------------------------------------------------------------------------

def test_nvenc_preview_emits_bf_zero():
    """-bf 0 must be emitted for h264_nvenc preview output."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    assert "-bf" in cmd
    assert cmd[cmd.index("-bf") + 1] == "0"


def test_nvenc_preview_emits_rc_cbr():
    """-rc cbr must be emitted for h264_nvenc preview output."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    assert "-rc" in cmd
    assert cmd[cmd.index("-rc") + 1] == "cbr"


def test_nvenc_preview_emits_repeat_headers():
    """-repeat_headers 1 must be emitted for h264_nvenc preview output."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    assert "-repeat_headers" in cmd
    assert cmd[cmd.index("-repeat_headers") + 1] == "1"


def test_nvenc_preview_bf_rc_repeat_headers_before_bitrate():
    """-bf, -rc, -repeat_headers must appear before -b:v in the preview section."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    idx_bf = cmd.index("-bf")
    idx_rc = cmd.index("-rc")
    idx_rh = cmd.index("-repeat_headers")
    bv_indices = [i for i, x in enumerate(cmd) if x == "-b:v"]
    # Last -b:v belongs to the preview output.
    last_bv = bv_indices[-1]
    assert idx_bf < last_bv
    assert idx_rc < last_bv
    assert idx_rh < last_bv


def test_nvenc_preview_bf_after_g():
    """-bf must appear after -g in the preview section."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    idx_g = cmd.index("-g")
    idx_bf = cmd.index("-bf")
    assert idx_bf > idx_g


# ---------------------------------------------------------------------------
# libx264 — new Phase 19 options
# ---------------------------------------------------------------------------

def test_libx264_preview_emits_x264_params():
    """-x264-params must be emitted for libx264 preview output."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    assert "-x264-params" in cmd


def test_libx264_preview_x264_params_repeat_headers():
    """-x264-params value must include repeat-headers=1."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "repeat-headers=1" in params


def test_libx264_preview_x264_params_keyint_equals_fps():
    """-x264-params keyint must equal rpo.fps."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "keyint=10" in params


def test_libx264_preview_x264_params_min_keyint_equals_fps():
    """-x264-params min-keyint must equal rpo.fps."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "min-keyint=10" in params


def test_libx264_preview_x264_params_scenecut_zero():
    """-x264-params value must include scenecut=0."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=10))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "scenecut=0" in params


def test_libx264_preview_x264_params_tracks_fps():
    """keyint/min-keyint in -x264-params must reflect a non-default fps."""
    cmd = build_ffmpeg_command(_cfg_libx264(fps=25))
    params = cmd[cmd.index("-x264-params") + 1]
    assert "keyint=25" in params
    assert "min-keyint=25" in params


def test_libx264_preview_emits_bf_zero():
    """-bf 0 must be emitted for libx264 preview output."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-bf" in cmd
    assert cmd[cmd.index("-bf") + 1] == "0"


def test_libx264_preview_no_rc_cbr():
    """-rc must NOT be emitted for libx264 (NVENC-only option)."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-rc" not in cmd


def test_libx264_preview_no_repeat_headers_flag():
    """-repeat_headers must NOT be emitted for libx264 (handled via -x264-params)."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-repeat_headers" not in cmd


# ---------------------------------------------------------------------------
# Both codecs — muxdelay / muxpreload
# ---------------------------------------------------------------------------

def test_nvenc_preview_emits_muxdelay():
    """-muxdelay 0 must be emitted for h264_nvenc preview output."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    assert "-muxdelay" in cmd
    assert cmd[cmd.index("-muxdelay") + 1] == "0"


def test_nvenc_preview_emits_muxpreload():
    """-muxpreload 0 must be emitted for h264_nvenc preview output."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    assert "-muxpreload" in cmd
    assert cmd[cmd.index("-muxpreload") + 1] == "0"


def test_libx264_preview_emits_muxdelay():
    """-muxdelay 0 must be emitted for libx264 preview output."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-muxdelay" in cmd
    assert cmd[cmd.index("-muxdelay") + 1] == "0"


def test_libx264_preview_emits_muxpreload():
    """-muxpreload 0 must be emitted for libx264 preview output."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-muxpreload" in cmd
    assert cmd[cmd.index("-muxpreload") + 1] == "0"


def test_nvenc_preview_muxdelay_after_format():
    """-muxdelay must appear after -f mpegts in the preview output section."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
    last_f_index = max(f_indices)
    idx_muxdelay = cmd.index("-muxdelay")
    assert idx_muxdelay > last_f_index


def test_nvenc_preview_muxpreload_after_muxdelay():
    """-muxpreload must appear after -muxdelay."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    idx_muxdelay = cmd.index("-muxdelay")
    idx_muxpreload = cmd.index("-muxpreload")
    assert idx_muxpreload > idx_muxdelay


def test_nvenc_preview_udp_url_after_muxpreload():
    """The UDP URL must be the last element, after -muxpreload."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    idx_muxpreload = cmd.index("-muxpreload")
    # The UDP URL is the final token.
    assert idx_muxpreload < len(cmd) - 1
    udp_url = cmd[-1]
    assert udp_url.startswith("udp://")


# ---------------------------------------------------------------------------
# rts1.json sanity
# ---------------------------------------------------------------------------

def test_rts1_build_command_has_bf_zero():
    """Full rts1 command build (h264_nvenc) must include -bf 0."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-bf" in cmd
    assert cmd[cmd.index("-bf") + 1] == "0"


def test_rts1_build_command_has_rc_cbr():
    """Full rts1 command build (h264_nvenc) must include -rc cbr."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-rc" in cmd
    assert cmd[cmd.index("-rc") + 1] == "cbr"


def test_rts1_build_command_has_repeat_headers():
    """Full rts1 command build (h264_nvenc) must include -repeat_headers 1."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-repeat_headers" in cmd
    assert cmd[cmd.index("-repeat_headers") + 1] == "1"


def test_rts1_build_command_has_muxdelay():
    """Full rts1 command build must include -muxdelay 0."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-muxdelay" in cmd
    assert cmd[cmd.index("-muxdelay") + 1] == "0"


def test_rts1_build_command_has_muxpreload():
    """Full rts1 command build must include -muxpreload 0."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-muxpreload" in cmd
    assert cmd[cmd.index("-muxpreload") + 1] == "0"
