"""
Phase 13 unit tests — Channel Config Reload.

Covers:
- ConfigReloadResponse schema
- channels API: POST /channels/{id}/reload-config
  - replaces DB config when JSON differs
  - is a no-op when JSON matches DB
  - 404 when JSON file missing
  - 422 when JSON file invalid
  - 422 when id mismatch in JSON file
  - updates name/display_name/enabled from JSON
- startup warning: _warn_db_config_differs_from_json
  - logs WARNING when DB differs from JSON
  - stays silent when DB matches JSON
  - stays silent when JSON file is missing
- frontend types completeness:
  - PreviewConfig.input_mode includes from_udp
  - ChannelConfig has recording_preview_output field
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models.schemas import (
    ChannelConfig,
    ConfigReloadResponse,
    PreviewConfig,
    RecordingPreviewOutputConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_config(channel_id: str = "rts1", name: str = "RTS1") -> ChannelConfig:
    return ChannelConfig.model_validate({
        "id": channel_id,
        "name": name,
        "display_name": f"{name} Display",
        "enabled": True,
        "ffmpeg_path": "ffmpeg",
        "capture": {
            "device_type": "dshow",
            "video_device": "Decklink Video Capture",
            "audio_device": "Decklink Audio Capture",
            "resolution": "720x576",
            "framerate": 25,
        },
        "encoding": {
            "video_codec": "libx264",
            "preset": "veryfast",
            "video_bitrate": "1500k",
            "audio_bitrate": "128k",
        },
        "filters": {
            "deinterlace": True,
            "scale_width": 1024,
            "scale_height": 576,
            "overlay": {"enabled": False},
        },
        "segmentation": {
            "segment_time": "00:05:00",
            "segment_atclocktime": True,
            "reset_timestamps": True,
            "strftime": True,
            "filename_pattern": "%d%m%y-%H%M%S",
        },
        "paths": {
            "record_dir": "D:/record/1_record",
            "chunks_dir": "D:/record/2_chunks",
            "final_dir": "D:/record/3_final",
        },
        "retention": {"enabled": True, "days": 30},
        "preview": {
            "enabled": False,
            "port": 23001,
            "scale": "320:180",
            "fps": 5,
            "input_mode": "from_udp",
        },
        "recording_preview_output": {
            "enabled": True,
            "url": "udp://127.0.0.1:23001?pkt_size=1316",
            "video_codec": "h264_nvenc",
            "audio_enabled": True,
        },
    })


def _write_channel_json(directory: Path, config: ChannelConfig) -> Path:
    p = directory / f"{config.id}.json"
    p.write_text(config.model_dump_json(), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_config_reload_response_fields():
    cfg = _minimal_config()
    r = ConfigReloadResponse(
        channel_id="rts1",
        config_changed=True,
        message="Config reloaded.",
        config=cfg,
    )
    assert r.channel_id == "rts1"
    assert r.config_changed is True
    assert r.config.id == "rts1"


def test_config_reload_response_no_change():
    cfg = _minimal_config()
    r = ConfigReloadResponse(
        channel_id="rts1",
        config_changed=False,
        message="Config unchanged.",
        config=cfg,
    )
    assert r.config_changed is False


# ---------------------------------------------------------------------------
# channels.py reload-config endpoint
# ---------------------------------------------------------------------------

def _make_db_channel(config: ChannelConfig):
    """Create a mock DB Channel row."""
    ch = MagicMock()
    ch.id = config.id
    ch.name = config.name
    ch.display_name = config.display_name
    ch.enabled = config.enabled
    ch.config_json = config.model_dump_json()
    return ch


def _run_reload(tmp_path: Path, db_config: ChannelConfig, json_config: ChannelConfig):
    """
    Exercise the reload_channel_config endpoint handler.

    Writes json_config to a temp file, mocks the DB, and calls the handler.
    Returns the ConfigReloadResponse.
    """
    from app.api.v1.channels import reload_channel_config

    _write_channel_json(tmp_path, json_config)

    db_ch = _make_db_channel(db_config)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = db_ch

    settings_mock = MagicMock()
    settings_mock.channels_config_dir = tmp_path

    with patch("app.config.settings.get_settings", return_value=settings_mock):
        result = reload_channel_config(json_config.id, db, MagicMock())

    return result, db_ch


def test_reload_config_detects_change(tmp_path):
    """When JSON differs from DB, config_changed=True and DB is updated."""
    old_cfg = _minimal_config(name="OLD")
    new_cfg = _minimal_config(name="NEW")

    result, db_ch = _run_reload(tmp_path, old_cfg, new_cfg)

    assert result.config_changed is True
    assert "reloaded" in result.message.lower()
    # The mock channel's config_json must have been updated
    assert db_ch.name == "NEW"
    assert db_ch.config_json == new_cfg.model_dump_json()


def test_reload_config_no_change(tmp_path):
    """When JSON matches DB, config_changed=False and no update is performed."""
    cfg = _minimal_config()

    result, db_ch = _run_reload(tmp_path, cfg, cfg)

    assert result.config_changed is False
    assert "unchanged" in result.message.lower()


def test_reload_config_updates_enabled_flag(tmp_path):
    """Reload must update the enabled flag when JSON changes it."""
    old_cfg = _minimal_config()
    new_data = json.loads(old_cfg.model_dump_json())
    new_data["enabled"] = False
    new_cfg = ChannelConfig.model_validate(new_data)

    result, db_ch = _run_reload(tmp_path, old_cfg, new_cfg)

    assert result.config_changed is True
    assert db_ch.enabled is False


def test_reload_config_404_when_json_missing(tmp_path):
    """Returns 404 when the JSON file does not exist."""
    from fastapi import HTTPException
    from app.api.v1.channels import reload_channel_config

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = _make_db_channel(_minimal_config())

    settings_mock = MagicMock()
    settings_mock.channels_config_dir = tmp_path  # empty dir — no JSON files

    with patch("app.config.settings.get_settings", return_value=settings_mock):
        with pytest.raises(HTTPException) as exc_info:
            reload_channel_config("rts1", db, MagicMock())

    assert exc_info.value.status_code == 404


def test_reload_config_422_when_json_invalid(tmp_path):
    """Returns 422 when the JSON file contains invalid config."""
    from fastapi import HTTPException
    from app.api.v1.channels import reload_channel_config

    bad_file = tmp_path / "rts1.json"
    bad_file.write_text("{this is not valid json}", encoding="utf-8")

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = _make_db_channel(_minimal_config())

    settings_mock = MagicMock()
    settings_mock.channels_config_dir = tmp_path

    with patch("app.config.settings.get_settings", return_value=settings_mock):
        with pytest.raises(HTTPException) as exc_info:
            reload_channel_config("rts1", db, MagicMock())

    assert exc_info.value.status_code == 422


def test_reload_config_422_on_id_mismatch(tmp_path):
    """Returns 422 when the id in the JSON file doesn't match the URL channel_id."""
    from fastapi import HTTPException
    from app.api.v1.channels import reload_channel_config

    # Write a config for "rts2" but request reload for "rts1"
    mismatched = _minimal_config(channel_id="rts2")
    _write_channel_json(tmp_path, mismatched)
    # Rename to rts1.json to simulate a copy/paste error
    (tmp_path / "rts2.json").rename(tmp_path / "rts1.json")

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = _make_db_channel(_minimal_config())

    settings_mock = MagicMock()
    settings_mock.channels_config_dir = tmp_path

    with patch("app.config.settings.get_settings", return_value=settings_mock):
        with pytest.raises(HTTPException) as exc_info:
            reload_channel_config("rts1", db, MagicMock())

    assert exc_info.value.status_code == 422
    assert "mismatch" in exc_info.value.detail.lower() or "rts2" in exc_info.value.detail


# ---------------------------------------------------------------------------
# _warn_db_config_differs_from_json startup check
# ---------------------------------------------------------------------------

def test_warn_db_config_differs_logs_warning(tmp_path, caplog):
    """Startup check logs WARNING when DB config differs from JSON file."""
    from app.main import _warn_db_config_differs_from_json

    old_cfg = _minimal_config(name="OLD")
    new_cfg = _minimal_config(name="NEW")
    _write_channel_json(tmp_path, new_cfg)

    db_ch = _make_db_channel(old_cfg)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = db_ch

    settings_mock = MagicMock()
    settings_mock.channels_config_dir = tmp_path

    with patch("app.main.get_settings", return_value=settings_mock):
        with caplog.at_level(logging.WARNING, logger="app.main"):
            _warn_db_config_differs_from_json(db)

    assert any("reload-config" in r.message for r in caplog.records), \
        f"Expected reload-config mention in warnings, got: {[r.message for r in caplog.records]}"


def test_warn_db_config_no_warning_when_same(tmp_path, caplog):
    """Startup check does NOT warn when DB config matches JSON file."""
    from app.main import _warn_db_config_differs_from_json

    cfg = _minimal_config()
    _write_channel_json(tmp_path, cfg)
    db_ch = _make_db_channel(cfg)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = db_ch

    settings_mock = MagicMock()
    settings_mock.channels_config_dir = tmp_path

    with patch("app.main.get_settings", return_value=settings_mock):
        with caplog.at_level(logging.WARNING, logger="app.main"):
            _warn_db_config_differs_from_json(db)

    # No warnings expected
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_warn_db_config_silent_when_no_json_dir(tmp_path, caplog):
    """Startup check is silent when channels_config_dir doesn't exist."""
    from app.main import _warn_db_config_differs_from_json

    settings_mock = MagicMock()
    settings_mock.channels_config_dir = tmp_path / "nonexistent"

    db = MagicMock()

    with patch("app.main.get_settings", return_value=settings_mock):
        with caplog.at_level(logging.WARNING, logger="app.main"):
            _warn_db_config_differs_from_json(db)

    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# PreviewConfig.input_mode includes from_udp
# ---------------------------------------------------------------------------

def test_preview_config_input_mode_from_udp():
    """PreviewConfig must accept from_udp as input_mode."""
    cfg = PreviewConfig(input_mode="from_udp")
    assert cfg.input_mode == "from_udp"


def test_preview_config_all_valid_modes():
    """All documented input_mode values must be accepted."""
    for mode in ("direct_capture", "from_recording_output", "from_udp", "disabled"):
        cfg = PreviewConfig(input_mode=mode)
        assert cfg.input_mode == mode


# ---------------------------------------------------------------------------
# ChannelConfig.recording_preview_output field
# ---------------------------------------------------------------------------

def test_channel_config_has_recording_preview_output_field():
    """recording_preview_output is Optional and defaults to None."""
    cfg = _minimal_config()
    # Remove recording_preview_output and check it defaults to None
    base_data = json.loads(cfg.model_dump_json())
    del base_data["recording_preview_output"]
    cfg2 = ChannelConfig.model_validate(base_data)
    assert cfg2.recording_preview_output is None


def test_channel_config_recording_preview_output_round_trip():
    """recording_preview_output survives a JSON round-trip."""
    cfg = _minimal_config()
    cfg2 = ChannelConfig.model_validate_json(cfg.model_dump_json())
    assert cfg2.recording_preview_output is not None
    assert cfg2.recording_preview_output.enabled is True
    assert cfg2.recording_preview_output.url == "udp://127.0.0.1:23001?pkt_size=1316"
    assert cfg2.preview.input_mode == "from_udp"
