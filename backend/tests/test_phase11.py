"""
Phase 11 unit tests — Channel Input Format Configuration.

Covers:
- CaptureConfig: new pixel_format / vcodec fields default to None
- CaptureConfig: full example with all fields set
- _build_capture_args: dshow uses -video_size (not -s)
- _build_capture_args: non-dshow uses -s
- _build_capture_args: pixel_format emitted only when set
- _build_capture_args: vcodec emitted only when set
- _build_capture_args: both optional fields emitted together
- _build_capture_args: argument order (before -i)
- build_ffmpeg_command: uses _build_capture_args (dshow -video_size)
- build_ffmpeg_command: pixel_format wired through
- build_hls_preview_command: dshow uses -video_size
- build_preview_command: dshow uses -video_size
- Channel config rts1.json: parses pixel_format correctly
- Channel config round-trip: JSON serialization preserves optional fields
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.schemas import CaptureConfig, ChannelConfig
from app.services.ffmpeg_builder import (
    _build_capture_args,
    build_ffmpeg_command,
    build_hls_preview_command,
    build_preview_command,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_channel_config(
    device_type: str = "dshow",
    resolution: str = "720x576",
    framerate: int = 25,
    pixel_format: str | None = None,
    vcodec: str | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id="test",
        name="TEST",
        display_name="Test Channel",
        capture=CaptureConfig(
            device_type=device_type,
            video_device="Decklink Video Capture",
            audio_device="Decklink Audio Capture",
            resolution=resolution,
            framerate=framerate,
            pixel_format=pixel_format,
            vcodec=vcodec,
        ),
        paths={
            "record_dir": "/tmp/rec",
            "chunks_dir": "/tmp/chunks",
            "final_dir": "/tmp/final",
        },
    )


def _load_channel_config(filename: str) -> ChannelConfig:
    base = Path(__file__).parent.parent / "data" / "channels"
    data = (base / filename).read_text(encoding="utf-8")
    return ChannelConfig.model_validate_json(data)


# ─── CaptureConfig schema ─────────────────────────────────────────────────────

def test_capture_config_defaults():
    cap = CaptureConfig()
    assert cap.pixel_format is None
    assert cap.vcodec is None
    assert cap.device_type == "dshow"
    assert cap.resolution == "720x576"
    assert cap.framerate == 25


def test_capture_config_with_pixel_format():
    cap = CaptureConfig(pixel_format="uyvy422")
    assert cap.pixel_format == "uyvy422"
    assert cap.vcodec is None


def test_capture_config_with_vcodec():
    cap = CaptureConfig(vcodec="rawvideo")
    assert cap.vcodec == "rawvideo"
    assert cap.pixel_format is None


def test_capture_config_full_hd():
    cap = CaptureConfig(
        device_type="dshow",
        video_device="Decklink Video Capture",
        audio_device="Decklink Audio Capture",
        resolution="1920x1080",
        framerate=50,
        pixel_format="uyvy422",
        vcodec=None,
    )
    assert cap.resolution == "1920x1080"
    assert cap.framerate == 50
    assert cap.pixel_format == "uyvy422"
    assert cap.vcodec is None


# ─── _build_capture_args ──────────────────────────────────────────────────────

def test_capture_args_dshow_uses_video_size():
    """dshow demuxer must use -video_size, not -s."""
    cfg = _make_channel_config(device_type="dshow", resolution="1920x1080")
    args = _build_capture_args(cfg)
    assert "-video_size" in args
    assert "1920x1080" == args[args.index("-video_size") + 1]
    assert "-s" not in args


def test_capture_args_v4l2_uses_s():
    """Non-dshow demuxers (v4l2) must use -s."""
    cfg = _make_channel_config(device_type="v4l2", resolution="1280x720")
    cfg.capture.video_device = "/dev/video0"
    args = _build_capture_args(cfg)
    assert "-s" in args
    assert "1280x720" == args[args.index("-s") + 1]
    assert "-video_size" not in args


def test_capture_args_framerate_present():
    cfg = _make_channel_config(framerate=50)
    args = _build_capture_args(cfg)
    assert "-framerate" in args
    assert "50" == args[args.index("-framerate") + 1]


def test_capture_args_no_pixel_format_by_default():
    cfg = _make_channel_config()
    args = _build_capture_args(cfg)
    assert "-pixel_format" not in args


def test_capture_args_pixel_format_emitted_when_set():
    cfg = _make_channel_config(pixel_format="uyvy422")
    args = _build_capture_args(cfg)
    assert "-pixel_format" in args
    assert "uyvy422" == args[args.index("-pixel_format") + 1]


def test_capture_args_no_vcodec_by_default():
    cfg = _make_channel_config()
    args = _build_capture_args(cfg)
    assert "-vcodec" not in args


def test_capture_args_vcodec_emitted_when_set():
    cfg = _make_channel_config(vcodec="rawvideo")
    args = _build_capture_args(cfg)
    assert "-vcodec" in args
    assert "rawvideo" == args[args.index("-vcodec") + 1]


def test_capture_args_both_optional_fields():
    cfg = _make_channel_config(pixel_format="nv12", vcodec="rawvideo")
    args = _build_capture_args(cfg)
    assert "-pixel_format" in args
    assert "-vcodec" in args


def test_capture_args_order_before_i():
    """All capture flags must appear before the -i flag."""
    cfg = _make_channel_config(pixel_format="uyvy422", vcodec="rawvideo")
    args = _build_capture_args(cfg)
    i_idx = args.index("-i")
    for flag in ("-f", "-video_size", "-framerate", "-pixel_format", "-vcodec"):
        assert flag in args
        assert args.index(flag) < i_idx, f"{flag} must come before -i"


def test_capture_args_dshow_i_value():
    """dshow -i value must be 'video=<name>:audio=<name>'."""
    cfg = _make_channel_config(device_type="dshow")
    cfg.capture.video_device = "My Video"
    cfg.capture.audio_device = "My Audio"
    args = _build_capture_args(cfg)
    i_value = args[args.index("-i") + 1]
    assert i_value == "video=My Video:audio=My Audio"


def test_capture_args_v4l2_i_value():
    """v4l2 -i value must be the device path directly."""
    cfg = _make_channel_config(device_type="v4l2")
    cfg.capture.video_device = "/dev/video0"
    args = _build_capture_args(cfg)
    i_value = args[args.index("-i") + 1]
    assert i_value == "/dev/video0"


# ─── build_ffmpeg_command ─────────────────────────────────────────────────────

def test_build_ffmpeg_command_dshow_video_size():
    """build_ffmpeg_command must use -video_size for dshow."""
    cfg = _make_channel_config(device_type="dshow", resolution="1920x1080")
    cmd = build_ffmpeg_command(cfg)
    assert "-video_size" in cmd
    assert cmd[cmd.index("-video_size") + 1] == "1920x1080"
    assert "-s" not in cmd


def test_build_ffmpeg_command_pixel_format_wired():
    cfg = _make_channel_config(pixel_format="uyvy422")
    cmd = build_ffmpeg_command(cfg)
    assert "-pixel_format" in cmd
    assert cmd[cmd.index("-pixel_format") + 1] == "uyvy422"


def test_build_ffmpeg_command_vcodec_wired():
    cfg = _make_channel_config(vcodec="rawvideo")
    cmd = build_ffmpeg_command(cfg)
    assert "-vcodec" in cmd
    assert cmd[cmd.index("-vcodec") + 1] == "rawvideo"


def test_build_ffmpeg_command_no_pixel_format_by_default():
    cfg = _make_channel_config()
    cmd = build_ffmpeg_command(cfg)
    assert "-pixel_format" not in cmd


# ─── build_hls_preview_command ────────────────────────────────────────────────

def test_build_hls_preview_command_dshow_video_size(tmp_path):
    cfg = _make_channel_config(device_type="dshow", resolution="1920x1080")
    cmd = build_hls_preview_command(cfg, tmp_path)
    assert "-video_size" in cmd
    assert cmd[cmd.index("-video_size") + 1] == "1920x1080"
    assert "-s" not in cmd[:cmd.index("-i")]


def test_build_hls_preview_command_pixel_format(tmp_path):
    cfg = _make_channel_config(pixel_format="uyvy422")
    cmd = build_hls_preview_command(cfg, tmp_path)
    assert "-pixel_format" in cmd
    assert cmd[cmd.index("-pixel_format") + 1] == "uyvy422"


# ─── build_preview_command ────────────────────────────────────────────────────

def test_build_preview_command_dshow_video_size():
    cfg = _make_channel_config(device_type="dshow", resolution="720x576")
    cmd = build_preview_command(cfg)
    assert "-video_size" in cmd
    assert cmd[cmd.index("-video_size") + 1] == "720x576"
    assert "-s" not in cmd[:cmd.index("-i")]


def test_build_preview_command_pixel_format():
    cfg = _make_channel_config(pixel_format="nv12")
    cmd = build_preview_command(cfg)
    assert "-pixel_format" in cmd
    assert cmd[cmd.index("-pixel_format") + 1] == "nv12"


# ─── HD / 1080i / 1080p / 720p configs ───────────────────────────────────────

@pytest.mark.parametrize("resolution,framerate", [
    ("720x576", 25),
    ("1920x1080", 25),
    ("1920x1080", 50),
    ("1280x720", 50),
])
def test_build_ffmpeg_command_various_resolutions(resolution, framerate):
    cfg = _make_channel_config(resolution=resolution, framerate=framerate)
    cmd = build_ffmpeg_command(cfg)
    assert cmd[cmd.index("-video_size") + 1] == resolution
    assert cmd[cmd.index("-framerate") + 1] == str(framerate)


# ─── Channel config JSON files ────────────────────────────────────────────────

def test_rts1_config_pixel_format():
    """rts1.json should now have pixel_format set (Phase 11 update)."""
    cfg = _load_channel_config("rts1.json")
    assert cfg.capture.pixel_format == "uyvy422"
    assert cfg.capture.vcodec is None


def test_rts1_config_round_trip():
    """Serializing and re-parsing rts1.json preserves pixel_format."""
    cfg = _load_channel_config("rts1.json")
    data = cfg.model_dump_json()
    cfg2 = ChannelConfig.model_validate_json(data)
    assert cfg2.capture.pixel_format == cfg.capture.pixel_format
    assert cfg2.capture.vcodec == cfg.capture.vcodec
    assert cfg2.capture.resolution == cfg.capture.resolution


def test_channel_config_without_new_fields_parses():
    """Existing channel configs without pixel_format/vcodec still parse (defaults to None)."""
    cfg = _load_channel_config("rts2.json")
    assert cfg.capture.pixel_format is None
    assert cfg.capture.vcodec is None


def test_channel_config_json_null_pixel_format():
    """Explicitly setting pixel_format=null in JSON produces None in Python."""
    raw = {
        "id": "test",
        "name": "T",
        "display_name": "T",
        "capture": {"pixel_format": None, "vcodec": None},
        "paths": {"record_dir": "/r", "chunks_dir": "/c", "final_dir": "/f"},
    }
    cfg = ChannelConfig.model_validate(raw)
    assert cfg.capture.pixel_format is None
    assert cfg.capture.vcodec is None
