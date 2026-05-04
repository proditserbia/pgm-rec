"""
Unit tests for drawtext filter escaping in -vf and -filter_complex modes.

Covers:
- _escape_time_format: no colon escaping in -vf mode
- _escape_time_format: colons escaped as \\: in filter_complex mode
- _escape_fontfile: Windows path remains correctly escaped
- _build_drawtext_filter: -vf mode produces plain colon in localtime macro
- _build_drawtext_filter: filter_complex mode escapes colons in localtime macro
  and in the time format string
- build_ffmpeg_command (-vf path): drawtext text uses plain colon
- build_ffmpeg_command (filter_complex path): drawtext text uses \\: for colons
- filter_complex string contains localtime\\: and %H\\:%M\\:%S substrings
"""
from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.schemas import ChannelConfig, OverlayConfig, RecordingPreviewOutputConfig
from app.services.ffmpeg_builder import (
    _build_drawtext_filter,
    _escape_fontfile,
    _escape_time_format,
    _build_filter_complex_with_preview,
    build_ffmpeg_command,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config_with_overlay(*, rpo_enabled: bool = False) -> ChannelConfig:
    """Minimal ChannelConfig with overlay enabled (default time_format)."""
    rpo = RecordingPreviewOutputConfig(enabled=True) if rpo_enabled else None
    return ChannelConfig(
        id="test",
        name="Test",
        display_name="Test Channel",
        capture={"device_type": "v4l2"},
        paths={
            "record_dir": "/tmp/rec",
            "chunks_dir": "/tmp/chunks",
            "final_dir": "/tmp/final",
        },
        recording_preview_output=rpo,
    )


# ---------------------------------------------------------------------------
# _escape_time_format
# ---------------------------------------------------------------------------

def test_escape_time_format_vf_mode_no_colon_escaping():
    """In -vf mode colons must not be escaped — single quotes protect them."""
    result = _escape_time_format("%d-%m-%y %H:%M:%S")
    assert result == "%d-%m-%y %H:%M:%S"


def test_escape_time_format_filter_complex_escapes_colons():
    """In filter_complex mode every colon must be escaped as \\:."""
    result = _escape_time_format("%d-%m-%y %H:%M:%S", for_filter_complex=True)
    assert result == r"%d-%m-%y %H\:%M\:%S"


def test_escape_time_format_vf_backslash_escaping():
    r"""Backslash is always escaped as \\ (both modes)."""
    result = _escape_time_format(r"test\path")
    assert result == r"test\\path"


def test_escape_time_format_filter_complex_backslash_then_colon():
    r"""In filter_complex mode backslash escaping happens before colon escaping."""
    # Backslash → \\, then colon → \:  (no double-escape of the added \)
    result = _escape_time_format(r"a\b:c", for_filter_complex=True)
    assert result == r"a\\b\:c"


def test_escape_time_format_single_quote_escaped():
    """Single quote is always escaped as \\' (both modes)."""
    result = _escape_time_format("it's")
    assert result == r"it\'s"


# ---------------------------------------------------------------------------
# _escape_fontfile
# ---------------------------------------------------------------------------

def test_escape_fontfile_windows_path():
    r"""Windows path C:\Windows\Fonts\verdana.ttf → 'C\:\\Windows\\Fonts\\verdana.ttf'"""
    result = _escape_fontfile(r"C:\Windows\Fonts\verdana.ttf")
    # backslash → \\  and  colon → \:  so the drive becomes C\:\\
    assert result == "'C\\:\\\\Windows\\\\Fonts\\\\verdana.ttf'"


def test_escape_fontfile_windows_path_colon_present():
    r"""The drive colon must be escaped as \: in fontfile."""
    result = _escape_fontfile(r"C:\Windows\Fonts\verdana.ttf")
    assert r"\:" in result


# ---------------------------------------------------------------------------
# _build_drawtext_filter — -vf mode
# ---------------------------------------------------------------------------

def test_build_drawtext_filter_vf_plain_colon_in_localtime():
    """In -vf mode the localtime separator colon is not escaped."""
    overlay = OverlayConfig(time_format="%d-%m-%y %H:%M:%S")
    result = _build_drawtext_filter(overlay, for_filter_complex=False)
    # text='%{localtime:%d-%m-%y %H:%M:%S}'
    assert "localtime:" in result
    assert "localtime\\:" not in result


def test_build_drawtext_filter_vf_no_escaped_colons_in_format():
    """In -vf mode time-format colons must not be escaped."""
    overlay = OverlayConfig(time_format="%H:%M:%S")
    result = _build_drawtext_filter(overlay, for_filter_complex=False)
    assert r"%H\:%M\:%S" not in result
    assert "%H:%M:%S" in result


# ---------------------------------------------------------------------------
# _build_drawtext_filter — filter_complex mode
# ---------------------------------------------------------------------------

def test_build_drawtext_filter_complex_escapes_localtime_sep():
    """In filter_complex mode the localtime separator colon must be escaped."""
    overlay = OverlayConfig(time_format="%d-%m-%y %H:%M:%S")
    result = _build_drawtext_filter(overlay, for_filter_complex=True)
    assert r"localtime\:" in result


def test_build_drawtext_filter_complex_escapes_format_colons():
    """In filter_complex mode colons inside the time format are escaped."""
    overlay = OverlayConfig(time_format="%H:%M:%S")
    result = _build_drawtext_filter(overlay, for_filter_complex=True)
    assert r"%H\:%M\:%S" in result


def test_build_drawtext_filter_complex_full_text_value():
    """End-to-end check: text value in filter_complex mode has all colons escaped."""
    overlay = OverlayConfig(time_format="%d-%m-%y %H:%M:%S")
    result = _build_drawtext_filter(overlay, for_filter_complex=True)
    expected_text = r"text='%{localtime\:%d-%m-%y %H\:%M\:%S}'"
    assert expected_text in result


# ---------------------------------------------------------------------------
# build_ffmpeg_command — single-output -vf path
# ---------------------------------------------------------------------------

def test_build_ffmpeg_command_vf_drawtext_plain_colon():
    """Single-output command uses -vf with unescaped colons in the time format."""
    cfg = _config_with_overlay(rpo_enabled=False)
    with patch("app.services.ffmpeg_builder.platform.system", return_value="Linux"):
        cmd = build_ffmpeg_command(cfg)
    # Find the -vf argument value
    vf_idx = cmd.index("-vf")
    vf_value = cmd[vf_idx + 1]
    assert "localtime:" in vf_value
    assert r"localtime\:" not in vf_value


# ---------------------------------------------------------------------------
# build_ffmpeg_command — filter_complex path (dual-output / preview)
# ---------------------------------------------------------------------------

def test_build_ffmpeg_command_filter_complex_escapes_colons():
    """filter_complex command has \\: for colons in localtime format."""
    cfg = _config_with_overlay(rpo_enabled=True)
    with patch("app.services.ffmpeg_builder.platform.system", return_value="Linux"):
        cmd = build_ffmpeg_command(cfg)
    fc_idx = cmd.index("-filter_complex")
    fc_value = cmd[fc_idx + 1]
    assert r"localtime\:" in fc_value
    assert r"%H\:%M\:%S" in fc_value


def test_build_filter_complex_contains_localtime_escaped_colon():
    """_build_filter_complex_with_preview produces the correct localtime\\: pattern."""
    cfg = _config_with_overlay(rpo_enabled=True)
    with patch("app.services.ffmpeg_builder.platform.system", return_value="Linux"):
        fc = _build_filter_complex_with_preview(cfg)
    assert r"localtime\:" in fc


def test_build_filter_complex_contains_time_format_escaped_colons():
    """_build_filter_complex_with_preview produces %H\\:%M\\:%S in the filter string."""
    cfg = _config_with_overlay(rpo_enabled=True)
    with patch("app.services.ffmpeg_builder.platform.system", return_value="Linux"):
        fc = _build_filter_complex_with_preview(cfg)
    assert r"%H\:%M\:%S" in fc


def test_build_filter_complex_no_plain_colon_in_localtime():
    """In filter_complex mode the localtime macro colon must always be escaped."""
    import re
    cfg = _config_with_overlay(rpo_enabled=True)
    with patch("app.services.ffmpeg_builder.platform.system", return_value="Linux"):
        fc = _build_filter_complex_with_preview(cfg)
    # An un-escaped localtime colon would match "localtime:" not preceded by "\"
    assert not re.search(r"(?<!\\)localtime:", fc), (
        f"Found un-escaped 'localtime:' in filter_complex string: {fc!r}"
    )
