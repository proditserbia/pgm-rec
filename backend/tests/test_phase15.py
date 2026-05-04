"""
Phase 15 unit tests — H.264 SPS/PPS repeat headers for UDP preview.

Covers:
- h264_nvenc preview output emits -forced-idr 1
- h264_nvenc preview output emits -g <fps>
- -g value equals rpo.fps (one IDR per second)
- libx264 preview output does NOT emit -forced-idr
- libx264 preview output DOES emit -g (set to fps*2 from Phase 20)
- rts1.json: switched to libx264 codec (Phase 20)
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


def _cfg_libx264(**rpo_kwargs) -> ChannelConfig:
    rpo_kwargs.setdefault("enabled", True)
    rpo_kwargs.setdefault("video_codec", "libx264")
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
# h264_nvenc — forced-idr and GOP
# ---------------------------------------------------------------------------

def test_nvenc_preview_emits_forced_idr():
    """h264_nvenc preview output must include -forced-idr 1."""
    cmd = build_ffmpeg_command(_cfg_nvenc())
    assert "-forced-idr" in cmd
    assert cmd[cmd.index("-forced-idr") + 1] == "1"


def test_nvenc_preview_emits_g_equal_to_fps():
    """h264_nvenc preview output must include -g equal to rpo.fps."""
    cmd = build_ffmpeg_command(_cfg_nvenc(fps=10))
    assert "-g" in cmd
    assert cmd[cmd.index("-g") + 1] == "10"


def test_nvenc_preview_g_reflects_custom_fps():
    """The -g value must track rpo.fps when a non-default value is used."""
    cmd = build_ffmpeg_command(_cfg_nvenc(fps=25))
    assert "-g" in cmd
    assert cmd[cmd.index("-g") + 1] == "25"


def test_nvenc_preview_forced_idr_before_bitrate():
    """
    -forced-idr / -g must appear before -b:v in the preview output section
    (i.e., grouped with codec options, not after the muxer flag).
    """
    cmd = build_ffmpeg_command(_cfg_nvenc())
    idx_idr = cmd.index("-forced-idr")
    idx_bv = [i for i, x in enumerate(cmd) if x == "-b:v"]
    # The last -b:v belongs to the preview output; -forced-idr must precede it.
    assert idx_idr < idx_bv[-1]


# ---------------------------------------------------------------------------
# libx264 — forced-idr and -g must NOT appear
# ---------------------------------------------------------------------------

def test_libx264_preview_no_forced_idr():
    """-forced-idr must NOT be emitted for libx264 preview output."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-forced-idr" not in cmd


def test_libx264_preview_emits_g():
    """-g IS emitted for libx264 preview output (set to fps*2 from Phase 20)."""
    cmd = build_ffmpeg_command(_cfg_libx264())
    assert "-g" in cmd


# ---------------------------------------------------------------------------
# rts1.json sanity
# ---------------------------------------------------------------------------

def test_rts1_uses_libx264_codec():
    """rts1.json uses libx264 codec (Phase 20 switch from h264_nvenc)."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output is not None
    assert cfg.recording_preview_output.video_codec == "libx264"


def test_rts1_build_command_has_no_forced_idr():
    """Full rts1 command build must NOT include -forced-idr (libx264 path)."""
    cfg = _load_rts1()
    cmd = build_ffmpeg_command(cfg)
    assert "-forced-idr" not in cmd


def test_rts1_build_command_has_g_equal_to_fps_times_two():
    """Full rts1 command build must include -g = fps*2 = 20 (libx264 path)."""
    cfg = _load_rts1()
    assert cfg.recording_preview_output.fps == 10
    cmd = build_ffmpeg_command(cfg)
    assert "-g" in cmd
    assert cmd[cmd.index("-g") + 1] == "20"
