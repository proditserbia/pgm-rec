"""
Phase 21 unit tests — UDP readiness probe before HLS FFmpeg start.

Covers:
- _probe_udp_stream returns True when FFmpeg probe exits with rc=0
- _probe_udp_stream returns False when FFmpeg probe exits with rc!=0
- _probe_udp_stream returns False on subprocess.TimeoutExpired
- _probe_udp_stream returns False on OSError (ffmpeg not found etc.)
- _probe_udp_stream uses -hide_banner flag
- _probe_udp_stream uses -t <probe_seconds> before -i
- _probe_udp_stream uses -f null as muxer
- _probe_udp_stream uses the provided listen_url as -i argument
- _probe_udp_stream uses the provided ffmpeg_path as the executable
- _probe_udp_stream uses default probe_seconds=3
- _probe_udp_stream honours custom probe_seconds
- _probe_udp_stream wall-clock timeout = probe_seconds + 5
- HlsPreviewManager._start_from_udp raises RuntimeError when probe fails
- RuntimeError message mentions listen_url
- RuntimeError message mentions recording / recording_preview_output
- HlsPreviewManager._start_from_udp proceeds when probe succeeds
- Probe is called after the port-availability check (port check still works)
- Probe uses config.ffmpeg_path
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from app.models.schemas import (
    ChannelConfig,
    PreviewConfig,
    RecordingPreviewOutputConfig,
)
from app.services.hls_preview_manager import (
    HlsPreviewManager,
    _probe_udp_stream,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LISTEN_URL = "udp://127.0.0.1:23001?overrun_nonfatal=1&fifo_size=50000000"


def _base_config(
    *,
    ffmpeg_path: str = "ffmpeg",
    listen_url: str = _LISTEN_URL,
    rpo_enabled: bool = True,
    input_mode: str = "from_udp",
    hls_mode: str = "auto",
) -> ChannelConfig:
    rpo = RecordingPreviewOutputConfig(
        enabled=rpo_enabled,
        url=_LISTEN_URL,
        listen_url=listen_url,
        video_codec="h264_nvenc",
        audio_enabled=True,
        audio_codec="aac",
        fail_safe_mode=False,
    )
    return ChannelConfig(
        id="rts1",
        name="RTS1",
        display_name="RTS1 Test",
        ffmpeg_path=ffmpeg_path,
        capture={"device_type": "dshow"},
        paths={
            "record_dir": "/tmp/rec",
            "chunks_dir": "/tmp/chunks",
            "final_dir": "/tmp/final",
        },
        recording_preview_output=rpo,
        preview=PreviewConfig(input_mode=input_mode, hls_mode=hls_mode),
    )


def _mock_process(returncode=None):
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 55555
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode
    return proc


def _completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


# ---------------------------------------------------------------------------
# _probe_udp_stream — return value
# ---------------------------------------------------------------------------

def test_probe_returns_true_on_rc_zero():
    """Probe must return True when FFmpeg exits with return code 0."""
    with patch("subprocess.run", return_value=_completed(0)):
        assert _probe_udp_stream("ffmpeg", _LISTEN_URL) is True


def test_probe_returns_false_on_rc_nonzero():
    """Probe must return False when FFmpeg exits with a non-zero return code."""
    with patch("subprocess.run", return_value=_completed(1)):
        assert _probe_udp_stream("ffmpeg", _LISTEN_URL) is False


def test_probe_returns_false_on_timeout():
    """Probe must return False when subprocess.TimeoutExpired is raised."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=8)):
        assert _probe_udp_stream("ffmpeg", _LISTEN_URL) is False


def test_probe_returns_false_on_oserror():
    """Probe must return False when OSError is raised (ffmpeg not found)."""
    with patch("subprocess.run", side_effect=OSError("not found")):
        assert _probe_udp_stream("ffmpeg", _LISTEN_URL) is False


# ---------------------------------------------------------------------------
# _probe_udp_stream — command structure
# ---------------------------------------------------------------------------

def test_probe_uses_hide_banner():
    """-hide_banner must appear in the probe command."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL)
    cmd = mock_run.call_args[0][0]
    assert "-hide_banner" in cmd


def test_probe_uses_t_flag_before_i():
    """-t must appear in the command before -i."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL, probe_seconds=3)
    cmd = mock_run.call_args[0][0]
    t_idx = cmd.index("-t")
    i_idx = cmd.index("-i")
    assert t_idx < i_idx


def test_probe_t_value_matches_probe_seconds():
    """-t value must equal probe_seconds."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL, probe_seconds=3)
    cmd = mock_run.call_args[0][0]
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "3"


def test_probe_uses_null_muxer():
    """-f null must appear in the probe command."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL)
    cmd = mock_run.call_args[0][0]
    assert "-f" in cmd
    f_idx = cmd.index("-f")
    assert cmd[f_idx + 1] == "null"


def test_probe_null_output_is_dash():
    """The output destination after -f null must be '-'."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL)
    cmd = mock_run.call_args[0][0]
    assert cmd[-1] == "-"


def test_probe_listen_url_passed_as_input():
    """The listen_url must be the argument following -i."""
    custom_url = "udp://127.0.0.1:29999?overrun_nonfatal=1"
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", custom_url)
    cmd = mock_run.call_args[0][0]
    i_idx = cmd.index("-i")
    assert cmd[i_idx + 1] == custom_url


def test_probe_uses_provided_ffmpeg_path():
    """The first element of the command must be the provided ffmpeg_path."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("/custom/ffmpeg", _LISTEN_URL)
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "/custom/ffmpeg"


# ---------------------------------------------------------------------------
# _probe_udp_stream — default and custom probe_seconds
# ---------------------------------------------------------------------------

def test_probe_default_seconds_is_3():
    """Default probe_seconds must be 3."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL)
    cmd = mock_run.call_args[0][0]
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "3"


def test_probe_custom_seconds():
    """Custom probe_seconds value must be reflected in the -t argument."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL, probe_seconds=5)
    cmd = mock_run.call_args[0][0]
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "5"


def test_probe_subprocess_timeout_is_probe_plus_five():
    """Wall-clock timeout passed to subprocess.run must be probe_seconds + 5."""
    with patch("subprocess.run", return_value=_completed(0)) as mock_run:
        _probe_udp_stream("ffmpeg", _LISTEN_URL, probe_seconds=3)
    _, kwargs = mock_run.call_args
    assert kwargs["timeout"] == 8  # 3 + 5


# ---------------------------------------------------------------------------
# HlsPreviewManager._start_from_udp — probe integration
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    return HlsPreviewManager()


def test_start_from_udp_raises_when_probe_fails(manager):
    """_start_from_udp must raise RuntimeError when the UDP probe returns False."""
    config = _base_config()

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="UDP stream not ready"):
            manager._start_from_udp("rts1", config)


def test_start_from_udp_error_mentions_listen_url(manager):
    """RuntimeError from a failed probe must contain the listen_url."""
    config = _base_config(listen_url=_LISTEN_URL)

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=False),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            manager._start_from_udp("rts1", config)
    assert "23001" in str(exc_info.value)


def test_start_from_udp_error_mentions_recording(manager):
    """RuntimeError from a failed probe must mention recording_preview_output."""
    config = _base_config()

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=False),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            manager._start_from_udp("rts1", config)
    assert "recording" in str(exc_info.value).lower()


def test_start_from_udp_proceeds_when_probe_succeeds(manager, tmp_path):
    """_start_from_udp must launch FFmpeg and return info when probe succeeds."""
    config = _base_config()
    mock_proc = _mock_process(returncode=None)

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=True),
        patch("app.services.hls_preview_manager.HlsPreviewManager._clean_output_dir"),
        patch("app.services.hls_preview_manager.HlsPreviewManager._new_log_path", return_value=Path("/tmp/test.log")),
        patch("builtins.open", MagicMock()),
        patch("subprocess.Popen", return_value=mock_proc),
    ):
        info = manager._start_from_udp("rts1", config)

    assert info is not None
    assert info.input_mode == "from_udp"


def test_start_from_udp_probe_called_with_config_ffmpeg_path(manager):
    """Probe must be called with config.ffmpeg_path as the executable."""
    config = _base_config(ffmpeg_path="/opt/ffmpeg/bin/ffmpeg")

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=False) as mock_probe,
    ):
        with pytest.raises(RuntimeError):
            manager._start_from_udp("rts1", config)

    mock_probe.assert_called_once()
    call_args = mock_probe.call_args[0]
    assert call_args[0] == "/opt/ffmpeg/bin/ffmpeg"


def test_start_from_udp_probe_called_with_listen_url(manager):
    """Probe must be called with the resolved listen_url."""
    config = _base_config(listen_url=_LISTEN_URL)

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", return_value=True),
        patch("app.services.hls_preview_manager._probe_udp_stream", return_value=False) as mock_probe,
    ):
        with pytest.raises(RuntimeError):
            manager._start_from_udp("rts1", config)

    call_args = mock_probe.call_args[0]
    assert call_args[1] == _LISTEN_URL


def test_start_from_udp_port_check_still_runs_before_probe(manager):
    """Port-availability check must happen before the UDP probe."""
    config = _base_config()
    call_order: list[str] = []

    def fake_port_check(host, port):
        call_order.append("port_check")
        return True

    def fake_probe(ffmpeg_path, listen_url, **kwargs):
        call_order.append("probe")
        return False

    with (
        patch("app.services.hls_preview_manager._check_udp_port_available", side_effect=fake_port_check),
        patch("app.services.hls_preview_manager._probe_udp_stream", side_effect=fake_probe),
    ):
        with pytest.raises(RuntimeError):
            manager._start_from_udp("rts1", config)

    assert call_order == ["port_check", "probe"]
