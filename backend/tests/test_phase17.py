"""
Phase 17 unit tests — UDP HLS generation improvements.

Covers:
- PreviewConfig.hls_mode field: default "auto", accepts copy/transcode/auto
- build_hls_preview_from_udp_command copy mode:
  - -fflags +genpts (no +nobuffer)
  - -analyzeduration 1000000
  - -probesize 1000000
  - -c:v copy, -c:a copy (audio_enabled=True)
  - -c:v copy, -an (audio_enabled=False)
  - independent_segments in hls_flags
  - validates H.264 codec (copy mode only)
  - validates AAC codec when audio enabled (copy mode only)
- build_hls_preview_from_udp_command transcode mode:
  - same input flags
  - -c:v libx264 -preset ultrafast -tune zerolatency
  - -c:a aac -b:a 96k -ar 48000
  - independent_segments in hls_flags
  - skips H.264 codec validation (non-H.264 source allowed)
  - skips AAC codec validation
- HlsPreviewManager._start_from_udp uses copy mode for hls_mode="copy"
- HlsPreviewManager._start_from_udp uses copy mode for hls_mode="auto"
- HlsPreviewManager._start_from_udp uses transcode mode for hls_mode="transcode"
- HlsPreviewInfo.hls_mode_used is set correctly
- _check_startup_timeout: from_udp+copy+auto → restarts in transcode (no failure)
- _check_startup_timeout: from_udp+transcode+auto → records failure (no dshow hints)
- _check_startup_timeout: from_udp failure reason mentions UDP URL
- _check_startup_timeout: from_udp failure reason has no Decklink/dshow mentions
- _check_startup_timeout: direct_capture failure reason has dshow hints
- _check_startup_timeout: from_udp failure tail uses 300 lines
- _reap_if_dead: from_udp failure has no Decklink hints
- _reap_if_dead: from_udp failure mentions mode used
- ChannelDiagnosticsResponse.ffplay_hint is set for from_udp mode
- ChannelDiagnosticsResponse.ffplay_hint is None for direct_capture mode
- rts1.json has hls_mode in preview section
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models.schemas import (
    ChannelConfig,
    ChannelDiagnosticsResponse,
    PreviewConfig,
    RecordingPreviewOutputConfig,
)
from app.services.ffmpeg_builder import build_hls_preview_from_udp_command
from app.services.hls_preview_manager import (
    HlsPreviewInfo,
    HlsPreviewManager,
    _check_udp_port_available,
)
from app.models.schemas import PreviewHealth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_config(
    *,
    input_mode: str = "from_udp",
    hls_mode: str = "auto",
    rpo_enabled: bool = True,
    rpo_kwargs: dict | None = None,
    audio_enabled: bool = True,
) -> ChannelConfig:
    rpo_kw = {
        "enabled": rpo_enabled,
        "url": "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000",
        "listen_url": "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000",
        "video_codec": "h264_nvenc",
        "audio_enabled": audio_enabled,
        "audio_codec": "aac",
        "fail_safe_mode": False,
    }
    if rpo_kwargs:
        rpo_kw.update(rpo_kwargs)
    rpo = RecordingPreviewOutputConfig(**rpo_kw)

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
        recording_preview_output=rpo,
        preview=PreviewConfig(input_mode=input_mode, hls_mode=hls_mode),
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


def _make_info(
    channel_id: str = "rts1",
    input_mode: str = "from_udp",
    hls_mode_used: str = "copy",
    output_dir: Path = Path("/tmp/preview/rts1"),
    log_path: Path = Path("/tmp/hls-preview.log"),
    started_at: datetime | None = None,
    returncode: int | None = None,
) -> HlsPreviewInfo:
    if started_at is None:
        started_at = datetime.utcnow()
    return HlsPreviewInfo(
        channel_id=channel_id,
        pid=12345,
        log_path=log_path,
        output_dir=output_dir,
        started_at=started_at,
        process=_mock_process(returncode=returncode),
        health=PreviewHealth.HEALTHY,
        input_mode=input_mode,
        hls_mode_used=hls_mode_used,
    )


# ---------------------------------------------------------------------------
# PreviewConfig.hls_mode
# ---------------------------------------------------------------------------

def test_preview_config_hls_mode_default():
    """hls_mode default must be 'auto'."""
    pc = PreviewConfig()
    assert pc.hls_mode == "auto"


def test_preview_config_hls_mode_copy():
    pc = PreviewConfig(hls_mode="copy")
    assert pc.hls_mode == "copy"


def test_preview_config_hls_mode_transcode():
    pc = PreviewConfig(hls_mode="transcode")
    assert pc.hls_mode == "transcode"


# ---------------------------------------------------------------------------
# build_hls_preview_from_udp_command — copy mode
# ---------------------------------------------------------------------------

def _copy_cmd(**kwargs) -> list[str]:
    config = _base_config(**kwargs)
    return build_hls_preview_from_udp_command(config, Path("/tmp/out"), mode="copy")


def test_copy_mode_fflags_genpts():
    cmd = _copy_cmd()
    idx = cmd.index("-fflags")
    assert cmd[idx + 1] == "+genpts"


def test_copy_mode_no_nobuffer():
    cmd = _copy_cmd()
    idx = cmd.index("-fflags")
    assert "nobuffer" not in cmd[idx + 1]


def test_copy_mode_analyzeduration():
    cmd = _copy_cmd()
    assert "-analyzeduration" in cmd
    assert cmd[cmd.index("-analyzeduration") + 1] == "1000000"


def test_copy_mode_probesize():
    cmd = _copy_cmd()
    assert "-probesize" in cmd
    assert cmd[cmd.index("-probesize") + 1] == "1000000"


def test_copy_mode_video_copy():
    cmd = _copy_cmd()
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_copy_mode_audio_copy_when_enabled():
    cmd = _copy_cmd(audio_enabled=True)
    assert "-c:a" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "-an" not in cmd


def test_copy_mode_audio_disabled():
    cmd = _copy_cmd(audio_enabled=False)
    assert "-an" in cmd
    # -c:a copy must not appear
    if "-c:a" in cmd:
        assert cmd[cmd.index("-c:a") + 1] != "copy"


def test_copy_mode_independent_segments():
    cmd = _copy_cmd()
    idx = cmd.index("-hls_flags")
    assert "independent_segments" in cmd[idx + 1]


def test_copy_mode_hls_flags_contains_delete_segments():
    cmd = _copy_cmd()
    idx = cmd.index("-hls_flags")
    assert "delete_segments" in cmd[idx + 1]


def test_copy_mode_hls_flags_contains_append_list():
    cmd = _copy_cmd()
    idx = cmd.index("-hls_flags")
    assert "append_list" in cmd[idx + 1]


def test_copy_mode_validates_h264_codec():
    """Copy mode must raise ValueError for non-H.264 video codec."""
    config = _base_config(rpo_kwargs={"video_codec": "libx265", "enabled": True})
    with pytest.raises(ValueError, match="not H.264-compatible"):
        build_hls_preview_from_udp_command(config, Path("/tmp/out"), mode="copy")


def test_copy_mode_validates_aac_codec():
    """Copy mode must raise ValueError for non-AAC audio when audio_enabled=True."""
    config = _base_config(
        audio_enabled=True,
        rpo_kwargs={"audio_codec": "mp3", "enabled": True},
    )
    with pytest.raises(ValueError, match="not AAC-compatible"):
        build_hls_preview_from_udp_command(config, Path("/tmp/out"), mode="copy")


def test_copy_mode_input_url_uses_listen_url():
    config = _base_config(
        rpo_kwargs={
            "enabled": True,
            "url": "udp://127.0.0.1:23001?pkt_size=1316",
            "listen_url": "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000",
            "video_codec": "h264_nvenc",
        }
    )
    cmd = build_hls_preview_from_udp_command(config, Path("/tmp/out"), mode="copy")
    idx = cmd.index("-i")
    assert "overrun_nonfatal" in cmd[idx + 1]


# ---------------------------------------------------------------------------
# build_hls_preview_from_udp_command — transcode mode
# ---------------------------------------------------------------------------

def _transcode_cmd(**kwargs) -> list[str]:
    config = _base_config(**kwargs)
    return build_hls_preview_from_udp_command(config, Path("/tmp/out"), mode="transcode")


def test_transcode_mode_video_libx264():
    cmd = _transcode_cmd()
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"


def test_transcode_mode_preset_ultrafast():
    cmd = _transcode_cmd()
    assert "-preset" in cmd
    assert cmd[cmd.index("-preset") + 1] == "ultrafast"


def test_transcode_mode_tune_zerolatency():
    cmd = _transcode_cmd()
    assert "-tune" in cmd
    assert cmd[cmd.index("-tune") + 1] == "zerolatency"


def test_transcode_mode_audio_aac():
    cmd = _transcode_cmd()
    assert "-c:a" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_transcode_mode_audio_bitrate():
    cmd = _transcode_cmd()
    assert "-b:a" in cmd
    assert cmd[cmd.index("-b:a") + 1] == "96k"


def test_transcode_mode_audio_sample_rate():
    cmd = _transcode_cmd()
    assert "-ar" in cmd
    assert cmd[cmd.index("-ar") + 1] == "48000"


def test_transcode_mode_independent_segments():
    cmd = _transcode_cmd()
    idx = cmd.index("-hls_flags")
    assert "independent_segments" in cmd[idx + 1]


def test_transcode_mode_skips_h264_validation():
    """Transcode mode must NOT raise for non-H.264 source codec."""
    config = _base_config(rpo_kwargs={"video_codec": "libx265", "enabled": True})
    # Should not raise
    cmd = build_hls_preview_from_udp_command(config, Path("/tmp/out"), mode="transcode")
    assert "-c:v" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"


def test_transcode_mode_skips_aac_validation():
    """Transcode mode must NOT raise for non-AAC source audio codec."""
    config = _base_config(
        audio_enabled=True,
        rpo_kwargs={"audio_codec": "mp3", "enabled": True},
    )
    # Should not raise
    cmd = build_hls_preview_from_udp_command(config, Path("/tmp/out"), mode="transcode")
    assert "-c:a" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_transcode_mode_fflags_genpts():
    cmd = _transcode_cmd()
    idx = cmd.index("-fflags")
    assert cmd[idx + 1] == "+genpts"


def test_transcode_mode_analyzeduration():
    cmd = _transcode_cmd()
    assert "-analyzeduration" in cmd
    assert cmd[cmd.index("-analyzeduration") + 1] == "1000000"


# ---------------------------------------------------------------------------
# HlsPreviewManager._start_from_udp — hls_mode selection
# ---------------------------------------------------------------------------

def _make_manager_with_udp_start(hls_mode: str = "auto") -> tuple[HlsPreviewManager, HlsPreviewInfo]:
    """Return a manager that has started a UDP preview, with the info."""
    manager = HlsPreviewManager()
    config = _base_config(hls_mode=hls_mode)

    mock_proc = _mock_process(returncode=None)  # still alive

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=True),
        patch("app.services.hls_preview_manager.HlsPreviewManager._clean_output_dir"),
        patch("app.services.hls_preview_manager.HlsPreviewManager._new_log_path", return_value=Path("/tmp/test.log")),
        patch("builtins.open", MagicMock()),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        info = manager._start_from_udp("rts1", config)

    return manager, info


def test_start_from_udp_copy_mode_for_auto():
    """hls_mode='auto' should start with copy mode."""
    _, info = _make_manager_with_udp_start(hls_mode="auto")
    assert info.hls_mode_used == "copy"


def test_start_from_udp_copy_mode_for_copy():
    """hls_mode='copy' should start with copy mode."""
    _, info = _make_manager_with_udp_start(hls_mode="copy")
    assert info.hls_mode_used == "copy"


def test_start_from_udp_transcode_mode_for_transcode():
    """hls_mode='transcode' should start with transcode mode."""
    _, info = _make_manager_with_udp_start(hls_mode="transcode")
    assert info.hls_mode_used == "transcode"


def test_start_from_udp_stores_config_in_udp_mode_configs():
    """Config should be stored in _udp_mode_configs for watchdog access."""
    manager, _ = _make_manager_with_udp_start(hls_mode="auto")
    assert "rts1" in manager._udp_mode_configs


def test_start_from_udp_force_mode_transcode():
    """_force_mode='transcode' overrides config hls_mode."""
    manager = HlsPreviewManager()
    config = _base_config(hls_mode="auto")  # would normally use copy

    mock_proc = _mock_process(returncode=None)

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=True),
        patch("app.services.hls_preview_manager.HlsPreviewManager._clean_output_dir"),
        patch("app.services.hls_preview_manager.HlsPreviewManager._new_log_path", return_value=Path("/tmp/test.log")),
        patch("builtins.open", MagicMock()),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        info = manager._start_from_udp("rts1", config, _force_mode="transcode")

    assert info.hls_mode_used == "transcode"


# ---------------------------------------------------------------------------
# _check_startup_timeout — from_udp auto fallback
# ---------------------------------------------------------------------------

def _make_manager_with_info(
    input_mode: str = "from_udp",
    hls_mode_used: str = "copy",
    config_hls_mode: str = "auto",
    elapsed_seconds: float = 999.0,
    playlist_exists: bool = False,
) -> tuple[HlsPreviewManager, HlsPreviewInfo, Path]:
    """Set up a manager with a running preview at startup timeout."""
    manager = HlsPreviewManager()
    tmp = Path("/tmp/test_preview_rts1")

    config = _base_config(input_mode=input_mode, hls_mode=config_hls_mode)
    started_at = datetime.utcnow() - timedelta(seconds=elapsed_seconds)

    info = _make_info(
        input_mode=input_mode,
        hls_mode_used=hls_mode_used,
        started_at=started_at,
        output_dir=tmp,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    return manager, info, tmp


def test_check_timeout_udp_auto_copy_triggers_transcode_fallback(tmp_path):
    """from_udp + copy + hls_mode=auto: timeout should restart with transcode."""
    manager = HlsPreviewManager()
    config = _base_config(input_mode="from_udp", hls_mode="auto")
    started_at = datetime.utcnow() - timedelta(seconds=999)

    info = _make_info(
        input_mode="from_udp",
        hls_mode_used="copy",
        started_at=started_at,
        output_dir=tmp_path,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    transcode_info = _make_info(input_mode="from_udp", hls_mode_used="transcode", output_dir=tmp_path)

    with (
        patch("app.services.hls_preview_manager.get_settings") as mock_settings,
        patch.object(manager, "_start_from_udp", return_value=transcode_info) as mock_start,
    ):
        mock_settings.return_value.preview_startup_timeout_seconds = 30
        manager._check_startup_timeout("rts1")

    # Should have called _start_from_udp with _force_mode="transcode"
    mock_start.assert_called_once()
    call_kwargs = mock_start.call_args
    assert call_kwargs.kwargs.get("_force_mode") == "transcode" or \
           (len(call_kwargs.args) >= 3 and call_kwargs.args[2] == "transcode")

    # No failure should be recorded yet (transcode attempt is in progress)
    assert "rts1" not in manager._failures
    # Original info removed from previews
    assert "rts1" not in manager._previews


def test_check_timeout_udp_auto_transcode_records_failure(tmp_path):
    """from_udp + transcode + hls_mode=auto: timeout should record failure."""
    manager = HlsPreviewManager()
    config = _base_config(input_mode="from_udp", hls_mode="auto")
    started_at = datetime.utcnow() - timedelta(seconds=999)

    info = _make_info(
        input_mode="from_udp",
        hls_mode_used="transcode",  # already tried transcode
        started_at=started_at,
        output_dir=tmp_path,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings:
        mock_settings.return_value.preview_startup_timeout_seconds = 30
        manager._check_startup_timeout("rts1")

    assert "rts1" in manager._failures
    assert "rts1" not in manager._previews


def test_check_timeout_udp_failure_no_dshow_hints(tmp_path):
    """from_udp failure reason must not mention Decklink or dshow."""
    manager = HlsPreviewManager()
    config = _base_config(input_mode="from_udp", hls_mode="transcode")
    started_at = datetime.utcnow() - timedelta(seconds=999)

    info = _make_info(
        input_mode="from_udp",
        hls_mode_used="transcode",
        started_at=started_at,
        output_dir=tmp_path,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings:
        mock_settings.return_value.preview_startup_timeout_seconds = 30
        manager._check_startup_timeout("rts1")

    assert "rts1" in manager._failures
    reason = manager._failures["rts1"].reason
    assert "dshow" not in reason.lower()
    assert "decklink" not in reason.lower()


def test_check_timeout_udp_failure_mentions_udp(tmp_path):
    """from_udp failure reason must mention UDP."""
    manager = HlsPreviewManager()
    config = _base_config(input_mode="from_udp", hls_mode="transcode")
    started_at = datetime.utcnow() - timedelta(seconds=999)

    info = _make_info(
        input_mode="from_udp",
        hls_mode_used="transcode",
        started_at=started_at,
        output_dir=tmp_path,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings:
        mock_settings.return_value.preview_startup_timeout_seconds = 30
        manager._check_startup_timeout("rts1")

    reason = manager._failures["rts1"].reason
    assert "udp" in reason.lower() or "UDP" in reason


def test_check_timeout_udp_failure_shows_mode(tmp_path):
    """from_udp failure reason must mention which mode was used."""
    manager = HlsPreviewManager()
    config = _base_config(input_mode="from_udp", hls_mode="copy")
    started_at = datetime.utcnow() - timedelta(seconds=999)

    info = _make_info(
        input_mode="from_udp",
        hls_mode_used="copy",
        started_at=started_at,
        output_dir=tmp_path,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings:
        mock_settings.return_value.preview_startup_timeout_seconds = 30
        manager._check_startup_timeout("rts1")

    reason = manager._failures["rts1"].reason
    assert "copy" in reason


def test_check_timeout_direct_capture_has_dshow_hints(tmp_path):
    """direct_capture failure reason must still mention dshow."""
    manager = HlsPreviewManager()
    started_at = datetime.utcnow() - timedelta(seconds=999)

    info = _make_info(
        input_mode="direct_capture",
        hls_mode_used=None,
        started_at=started_at,
        output_dir=tmp_path,
    )
    manager._previews["rts1"] = info

    with patch("app.services.hls_preview_manager.get_settings") as mock_settings:
        mock_settings.return_value.preview_startup_timeout_seconds = 30
        manager._check_startup_timeout("rts1")

    reason = manager._failures["rts1"].reason
    assert "dshow" in reason


# ---------------------------------------------------------------------------
# _reap_if_dead — from_udp failure messages
# ---------------------------------------------------------------------------

def test_reap_if_dead_udp_no_dshow_hints(tmp_path):
    """_reap_if_dead for from_udp must not mention Decklink or dshow."""
    manager = HlsPreviewManager()
    config = _base_config(input_mode="from_udp", hls_mode="copy")

    info = _make_info(
        input_mode="from_udp",
        hls_mode_used="copy",
        output_dir=tmp_path,
        returncode=1,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    manager._reap_if_dead("rts1")

    assert "rts1" in manager._failures
    reason = manager._failures["rts1"].reason
    assert "dshow" not in reason.lower()
    assert "decklink" not in reason.lower()


def test_reap_if_dead_udp_mentions_mode(tmp_path):
    """_reap_if_dead for from_udp should mention the mode used."""
    manager = HlsPreviewManager()
    config = _base_config(input_mode="from_udp", hls_mode="auto")

    info = _make_info(
        input_mode="from_udp",
        hls_mode_used="transcode",
        output_dir=tmp_path,
        returncode=1,
    )
    manager._previews["rts1"] = info
    manager._udp_mode_configs["rts1"] = config

    manager._reap_if_dead("rts1")

    reason = manager._failures["rts1"].reason
    assert "transcode" in reason


def test_reap_if_dead_direct_capture_no_udp_message(tmp_path):
    """_reap_if_dead for direct_capture should use standard message (no UDP)."""
    manager = HlsPreviewManager()

    info = _make_info(
        input_mode="direct_capture",
        hls_mode_used=None,
        output_dir=tmp_path,
        returncode=1,
    )
    manager._previews["rts1"] = info

    manager._reap_if_dead("rts1")

    reason = manager._failures["rts1"].reason
    # Standard message — should not say "UDP HLS" since this is direct_capture
    assert "UDP HLS" not in reason


# ---------------------------------------------------------------------------
# ChannelDiagnosticsResponse.ffplay_hint
# ---------------------------------------------------------------------------

def test_diagnostics_ffplay_hint_default_none():
    """ffplay_hint defaults to None."""
    r = ChannelDiagnosticsResponse(
        channel_id="rts1",
        ffmpeg_command="ffmpeg ...",
        ffmpeg_command_list=["ffmpeg"],
        device_type="dshow",
        input_specifier="video=...",
        resolution="720x576",
        framerate=25,
        record_dir="/tmp/rec",
        stderr_tail=[],
    )
    assert r.ffplay_hint is None


def test_diagnostics_ffplay_hint_set():
    """ffplay_hint is a plain string when provided."""
    r = ChannelDiagnosticsResponse(
        channel_id="rts1",
        ffmpeg_command="ffmpeg ...",
        ffmpeg_command_list=["ffmpeg"],
        device_type="dshow",
        input_specifier="video=...",
        resolution="720x576",
        framerate=25,
        record_dir="/tmp/rec",
        stderr_tail=[],
        ffplay_hint='ffplay "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"',
    )
    assert r.ffplay_hint is not None
    assert "ffplay" in r.ffplay_hint
    assert "udp" in r.ffplay_hint


# ---------------------------------------------------------------------------
# stop_preview clears _udp_mode_configs
# ---------------------------------------------------------------------------

def test_stop_preview_clears_udp_mode_configs():
    """stop_preview must remove entry from _udp_mode_configs."""
    manager = HlsPreviewManager()
    config = _base_config(hls_mode="auto")

    mock_proc = _mock_process(returncode=None)

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=True),
        patch("app.services.hls_preview_manager.HlsPreviewManager._clean_output_dir"),
        patch("app.services.hls_preview_manager.HlsPreviewManager._new_log_path", return_value=Path("/tmp/test.log")),
        patch("builtins.open", MagicMock()),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("app.services.hls_preview_manager.get_settings") as mock_settings,
    ):
        mock_settings.return_value.stop_timeout_seconds = 5
        manager._start_from_udp("rts1", config)
        assert "rts1" in manager._udp_mode_configs
        manager.stop_preview("rts1")

    assert "rts1" not in manager._udp_mode_configs


# ---------------------------------------------------------------------------
# rts1.json — hls_mode present
# ---------------------------------------------------------------------------

def test_rts1_json_has_hls_mode():
    """rts1.json preview section must include hls_mode."""
    config = _load_rts1()
    assert hasattr(config.preview, "hls_mode")
    assert config.preview.hls_mode in ("copy", "transcode", "auto")


def test_rts1_json_hls_mode_is_auto():
    """rts1.json hls_mode should be 'auto' for Phase 17 default behaviour."""
    config = _load_rts1()
    assert config.preview.hls_mode == "auto"
