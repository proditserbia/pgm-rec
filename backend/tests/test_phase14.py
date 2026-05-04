"""
Phase 14 unit tests — UDP preview URL split and per-channel port support.

Covers:
- RecordingPreviewOutputConfig: send_url and listen_url fields (default None)
- Backward compatibility: url still used when send_url/listen_url not set
- build_ffmpeg_command with preview: uses send_url when set
- build_ffmpeg_command with preview: falls back to url when send_url is None
- build_hls_preview_from_udp_command: uses listen_url when set
- build_hls_preview_from_udp_command: falls back to url when listen_url is None
- PreviewConfig: udp_port field (default None)
- PreviewConfig: udp_port can be set per channel
- _check_udp_port_available: returns True for a free port
- _check_udp_port_available: returns False for an occupied port
- _extract_udp_host_port: correct parsing of udp:// URLs
- _extract_udp_host_port: returns None for non-udp URLs
- HlsPreviewManager._start_from_udp: raises RuntimeError if port occupied
- HlsPreviewManager._start_from_udp: skips check when URL can't be parsed
- rts1.json Phase 14 update: send_url, listen_url, udp_port present
"""
from __future__ import annotations

import socket
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
    build_ffmpeg_command,
    build_hls_preview_from_udp_command,
)
from app.services.hls_preview_manager import (
    HlsPreviewManager,
    _check_udp_port_available,
    _extract_udp_host_port,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_config(
    *,
    rpo_enabled: bool = False,
    rpo_kwargs: dict | None = None,
) -> ChannelConfig:
    rpo = None
    if rpo_kwargs is not None or rpo_enabled:
        rpo_kwargs = rpo_kwargs or {}
        rpo_kwargs.setdefault("enabled", rpo_enabled)
        rpo = RecordingPreviewOutputConfig(**rpo_kwargs)

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
# RecordingPreviewOutputConfig — send_url / listen_url fields
# ---------------------------------------------------------------------------

def test_rpo_send_url_default_none():
    rpo = RecordingPreviewOutputConfig()
    assert rpo.send_url is None


def test_rpo_listen_url_default_none():
    rpo = RecordingPreviewOutputConfig()
    assert rpo.listen_url is None


def test_rpo_send_url_can_be_set():
    rpo = RecordingPreviewOutputConfig(send_url="udp://127.0.0.1:23001?pkt_size=1316")
    assert rpo.send_url == "udp://127.0.0.1:23001?pkt_size=1316"


def test_rpo_listen_url_can_be_set():
    rpo = RecordingPreviewOutputConfig(
        listen_url="udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"
    )
    assert rpo.listen_url == "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"


def test_rpo_send_and_listen_url_independent():
    rpo = RecordingPreviewOutputConfig(
        send_url="udp://127.0.0.1:23001?pkt_size=1316",
        listen_url="udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000",
    )
    assert rpo.send_url != rpo.listen_url


def test_rpo_url_still_defaults_for_backward_compat():
    rpo = RecordingPreviewOutputConfig()
    assert rpo.url == "udp://127.0.0.1:23001?pkt_size=1316"


def test_rpo_round_trip_with_send_and_listen_url():
    rpo = RecordingPreviewOutputConfig(
        enabled=True,
        send_url="udp://127.0.0.1:23002?pkt_size=1316",
        listen_url="udp://127.0.0.1:23002?overrun_nonfatal=1&fifo_size=50000000",
    )
    data = rpo.model_dump_json()
    rpo2 = RecordingPreviewOutputConfig.model_validate_json(data)
    assert rpo2.send_url == rpo.send_url
    assert rpo2.listen_url == rpo.listen_url


# ---------------------------------------------------------------------------
# build_ffmpeg_command — send_url used by recording output
# ---------------------------------------------------------------------------

def test_build_ffmpeg_command_uses_send_url_when_set():
    """When send_url is set, it must appear in the recording command."""
    send = "udp://127.0.0.1:23001?pkt_size=1316"
    cfg = _base_config(rpo_kwargs={"enabled": True, "send_url": send})
    cmd = build_ffmpeg_command(cfg)
    assert send in cmd


def test_build_ffmpeg_command_send_url_not_listen_url_in_cmd():
    """listen_url must NOT appear in the recording command."""
    listen = "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "send_url": "udp://127.0.0.1:23001?pkt_size=1316",
            "listen_url": listen,
        }
    )
    cmd = build_ffmpeg_command(cfg)
    assert listen not in cmd


def test_build_ffmpeg_command_fallback_to_url_when_send_url_none():
    """When send_url is None, url is used as the recording output URL."""
    legacy_url = "udp://127.0.0.1:23001?pkt_size=1316"
    cfg = _base_config(
        rpo_kwargs={"enabled": True, "url": legacy_url, "send_url": None}
    )
    cmd = build_ffmpeg_command(cfg)
    assert legacy_url in cmd


def test_build_ffmpeg_command_send_url_overrides_url():
    """send_url takes priority over url when both are set."""
    legacy_url = "udp://127.0.0.1:23001?pkt_size=1316"
    send_url = "udp://127.0.0.1:23002?pkt_size=1316"
    cfg = _base_config(
        rpo_kwargs={"enabled": True, "url": legacy_url, "send_url": send_url}
    )
    cmd = build_ffmpeg_command(cfg)
    assert send_url in cmd
    assert legacy_url not in cmd


# ---------------------------------------------------------------------------
# build_hls_preview_from_udp_command — listen_url used by HLS receiver
# ---------------------------------------------------------------------------

def test_build_hls_preview_uses_listen_url_when_set(tmp_path):
    """When listen_url is set, it must be the -i input in the HLS preview command."""
    listen = "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"
    cfg = _base_config(rpo_kwargs={"enabled": True, "listen_url": listen})
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    assert listen in cmd


def test_build_hls_preview_listen_url_not_send_url_in_cmd(tmp_path):
    """send_url must NOT appear as the HLS preview input."""
    send = "udp://127.0.0.1:23001?pkt_size=1316"
    listen = "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"
    cfg = _base_config(rpo_kwargs={"enabled": True, "send_url": send, "listen_url": listen})
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    # listen_url must be present, send_url must not be the -i input
    i_idx = cmd.index("-i")
    assert cmd[i_idx + 1] == listen


def test_build_hls_preview_fallback_to_url_when_listen_url_none(tmp_path):
    """When listen_url is None, url is used as the HLS preview input."""
    legacy_url = "udp://127.0.0.1:23001?pkt_size=1316"
    cfg = _base_config(
        rpo_kwargs={"enabled": True, "url": legacy_url, "listen_url": None}
    )
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    i_idx = cmd.index("-i")
    assert cmd[i_idx + 1] == legacy_url


def test_build_hls_preview_listen_url_overrides_url(tmp_path):
    """listen_url takes priority over url when both are set."""
    legacy_url = "udp://127.0.0.1:23001?pkt_size=1316"
    listen_url = "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"
    cfg = _base_config(
        rpo_kwargs={"enabled": True, "url": legacy_url, "listen_url": listen_url}
    )
    cmd = build_hls_preview_from_udp_command(cfg, tmp_path)
    i_idx = cmd.index("-i")
    assert cmd[i_idx + 1] == listen_url
    assert legacy_url not in cmd


# ---------------------------------------------------------------------------
# PreviewConfig — udp_port field
# ---------------------------------------------------------------------------

def test_preview_config_udp_port_default_none():
    p = PreviewConfig()
    assert p.udp_port is None


def test_preview_config_udp_port_can_be_set():
    p = PreviewConfig(udp_port=23001)
    assert p.udp_port == 23001


def test_preview_config_udp_port_per_channel():
    """Different channels can use different ports."""
    p1 = PreviewConfig(udp_port=23001)
    p2 = PreviewConfig(udp_port=23002)
    p3 = PreviewConfig(udp_port=23003)
    assert len({p1.udp_port, p2.udp_port, p3.udp_port}) == 3


def test_preview_config_udp_port_round_trip():
    p = PreviewConfig(udp_port=23004)
    data = p.model_dump_json()
    p2 = PreviewConfig.model_validate_json(data)
    assert p2.udp_port == 23004


# ---------------------------------------------------------------------------
# _check_udp_port_available helper
# ---------------------------------------------------------------------------

def test_check_udp_port_available_free_port():
    """A port that is not bound should be reported as available."""
    # Find a free port by temporarily binding to it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    # The port should now be free.
    assert _check_udp_port_available("127.0.0.1", free_port) is True


def test_check_udp_port_available_occupied_port():
    """A port that is already bound should be reported as occupied."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    occupied_port = sock.getsockname()[1]
    try:
        assert _check_udp_port_available("127.0.0.1", occupied_port) is False
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# _extract_udp_host_port helper
# ---------------------------------------------------------------------------

def test_extract_udp_host_port_standard():
    result = _extract_udp_host_port("udp://127.0.0.1:23001?pkt_size=1316")
    assert result == ("127.0.0.1", 23001)


def test_extract_udp_host_port_with_listen_options():
    result = _extract_udp_host_port(
        "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"
    )
    assert result == ("127.0.0.1", 23001)


def test_extract_udp_host_port_different_port():
    result = _extract_udp_host_port("udp://127.0.0.1:23002?pkt_size=1316")
    assert result == ("127.0.0.1", 23002)


def test_extract_udp_host_port_non_udp_returns_none():
    result = _extract_udp_host_port("rtp://127.0.0.1:5004")
    assert result is None


def test_extract_udp_host_port_no_port_returns_none():
    result = _extract_udp_host_port("udp://127.0.0.1")
    assert result is None


def test_extract_udp_host_port_invalid_returns_none():
    result = _extract_udp_host_port("not-a-url")
    assert result is None


# ---------------------------------------------------------------------------
# HlsPreviewManager — preflight port check
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    return HlsPreviewManager()


def test_manager_start_from_udp_raises_when_port_occupied(manager, tmp_path):
    """
    _start_from_udp must raise RuntimeError with a clear message if the UDP
    listen port is already occupied by another process.
    """
    # Occupy a port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    occupied_port = sock.getsockname()[1]
    try:
        listen_url = f"udp://127.0.0.1:{occupied_port}?overrun_nonfatal=1&fifo_size=50000000"
        cfg = _base_config(
            rpo_kwargs={
                "enabled": True,
                "send_url": f"udp://127.0.0.1:{occupied_port}?pkt_size=1316",
                "listen_url": listen_url,
            }
        )
        cfg.preview.input_mode = "from_udp"

        with pytest.raises(RuntimeError, match="already in use"):
            manager.start_preview("rts1", cfg)
    finally:
        sock.close()


def test_manager_start_from_udp_raises_message_includes_port(manager, tmp_path):
    """The RuntimeError message should mention the port number."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    occupied_port = sock.getsockname()[1]
    try:
        listen_url = f"udp://127.0.0.1:{occupied_port}?overrun_nonfatal=1&fifo_size=50000000"
        cfg = _base_config(
            rpo_kwargs={"enabled": True, "listen_url": listen_url}
        )
        cfg.preview.input_mode = "from_udp"

        with pytest.raises(RuntimeError, match=str(occupied_port)):
            manager.start_preview("rts1", cfg)
    finally:
        sock.close()


def test_manager_start_from_udp_raises_message_mentions_ffplay(manager):
    """The RuntimeError message should advise stopping ffplay/ffmpeg."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    occupied_port = sock.getsockname()[1]
    try:
        listen_url = f"udp://127.0.0.1:{occupied_port}?overrun_nonfatal=1"
        cfg = _base_config(rpo_kwargs={"enabled": True, "listen_url": listen_url})
        cfg.preview.input_mode = "from_udp"

        with pytest.raises(RuntimeError, match="ffplay"):
            manager.start_preview("rts1", cfg)
    finally:
        sock.close()


def test_manager_start_from_udp_proceeds_on_free_port(manager, tmp_path):
    """If the port is free, _start_from_udp should proceed and launch FFmpeg."""
    # Find a free port (bind, get port, close socket so it's free).
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    listen_url = f"udp://127.0.0.1:{free_port}?overrun_nonfatal=1&fifo_size=50000000"
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "send_url": f"udp://127.0.0.1:{free_port}?pkt_size=1316",
            "listen_url": listen_url,
        }
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
    assert info.input_mode == "from_udp"


def test_manager_start_from_udp_skips_preflight_for_unparseable_url(manager, tmp_path):
    """If listen_url can't be parsed (no port), preflight is skipped gracefully."""
    # Use a non-udp scheme — preflight will be skipped.
    cfg = _base_config(
        rpo_kwargs={
            "enabled": True,
            "url": "udp://127.0.0.1:23099?pkt_size=1316",
            "listen_url": None,  # will fall back to url
        }
    )
    cfg.preview.input_mode = "from_udp"
    mock_proc = _mock_process()

    # The url is parseable so preflight runs; use an actually free port.
    # Redirect listen to a custom unparseable string via monkeypatching helper.
    with patch(
        "app.services.hls_preview_manager._extract_udp_host_port",
        return_value=None,  # simulate unparseable URL
    ), patch("app.services.hls_preview_manager.get_settings") as mock_settings, \
       patch("subprocess.Popen", return_value=mock_proc):
        ms = MagicMock()
        ms.logs_dir = tmp_path / "logs"
        ms.preview_dir = tmp_path / "preview"
        ms.logs_dir.mkdir(parents=True, exist_ok=True)
        ms.preview_dir.mkdir(parents=True, exist_ok=True)
        mock_settings.return_value = ms

        info = manager.start_preview("rts1", cfg)

    assert info is not None


# ---------------------------------------------------------------------------
# rts1.json Phase 14 update
# ---------------------------------------------------------------------------

def test_rts1_has_send_url():
    cfg = _load_rts1()
    assert cfg.recording_preview_output is not None
    assert cfg.recording_preview_output.send_url is not None


def test_rts1_send_url_is_sender_style():
    cfg = _load_rts1()
    send = cfg.recording_preview_output.send_url
    assert "pkt_size" in send


def test_rts1_has_listen_url():
    cfg = _load_rts1()
    assert cfg.recording_preview_output.listen_url is not None


def test_rts1_listen_url_is_receiver_style():
    cfg = _load_rts1()
    listen = cfg.recording_preview_output.listen_url
    assert "overrun_nonfatal" in listen or "fifo_size" in listen


def test_rts1_send_url_and_listen_url_differ():
    cfg = _load_rts1()
    rpo = cfg.recording_preview_output
    assert rpo.send_url != rpo.listen_url


def test_rts1_preview_has_udp_port():
    cfg = _load_rts1()
    assert cfg.preview.udp_port is not None


def test_rts1_preview_udp_port_matches_url_port():
    cfg = _load_rts1()
    udp_port = cfg.preview.udp_port
    addr = _extract_udp_host_port(cfg.recording_preview_output.send_url)
    assert addr is not None
    assert addr[1] == udp_port


def test_rts1_round_trip_with_phase14_fields():
    cfg = _load_rts1()
    data = cfg.model_dump_json()
    cfg2 = ChannelConfig.model_validate_json(data)
    assert cfg2.recording_preview_output.send_url == cfg.recording_preview_output.send_url
    assert cfg2.recording_preview_output.listen_url == cfg.recording_preview_output.listen_url
    assert cfg2.preview.udp_port == cfg.preview.udp_port
