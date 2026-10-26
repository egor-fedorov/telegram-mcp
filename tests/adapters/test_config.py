import os
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from telegram_mcp.adapters.config import Settings, load_settings, save_settings


def test_configuration_roundtrip_and_file_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_PATH"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.toml"
    settings = Settings(
        api_id=123,
        api_hash=SecretStr("example-not-a-real-key"),
        session_path=tmp_path / "test.session",
        allowed_folder_ids=[1],
        allow_public_channel_reads=True,
        allow_public_channel_discovery=True,
    )
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.credentials() == (123, "example-not-a-real-key")
    assert loaded.allowed_folder_ids == [1]
    assert loaded.allow_public_channel_reads is True
    assert loaded.allow_public_channel_discovery is True
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "example-not-a-real-key" not in repr(loaded)


def test_public_channels_require_explicit_configuration(tmp_path: Path) -> None:
    assert Settings().allow_public_channel_reads is False
    assert Settings().allow_public_channel_discovery is False
    path = tmp_path / "config.toml"
    path.write_text('allowed_chat_ids = ["1"]\n')
    assert load_settings(path).allow_public_channel_reads is False
    assert load_settings(path).allow_public_channel_discovery is False


def test_discovery_permission_is_independent_of_read_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("allow_all_chats = true\nallow_public_channel_reads = true\n")
    assert load_settings(path).allow_public_channel_discovery is False
    path.write_text("allow_public_channel_discovery = true\n")
    settings = load_settings(path)
    assert settings.allow_public_channel_discovery is True
    assert settings.allow_public_channel_reads is False
    assert settings.allow_all_chats is False
    assert settings.allowed_chat_ids == []
    assert settings.allowed_folder_ids == []


def test_attachment_permission_defaults_off_and_roundtrips(tmp_path: Path) -> None:
    assert not Settings().allow_attachment_downloads
    path = tmp_path / "config.toml"
    save_settings(Settings(allow_attachment_downloads=True), path)
    assert load_settings(path).allow_attachment_downloads


@pytest.mark.parametrize("ttl,stale", [(0, 300), (300, 0), (30, 120), (300, 300)])
def test_catalog_age_settings_roundtrip(tmp_path: Path, ttl: int, stale: int) -> None:
    assert Settings().catalog_cache_ttl_seconds == 300
    assert Settings().catalog_cache_stale_seconds == 300
    path = tmp_path / "config.toml"
    save_settings(Settings(catalog_cache_ttl_seconds=ttl, catalog_cache_stale_seconds=stale), path)
    settings = load_settings(path)
    assert settings.catalog_cache_ttl_seconds == ttl
    assert settings.catalog_cache_stale_seconds == stale


@pytest.mark.parametrize("value", [-1, 301])
def test_catalog_stale_age_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError, match="catalog_cache_stale_seconds"):
        Settings(catalog_cache_stale_seconds=value)
