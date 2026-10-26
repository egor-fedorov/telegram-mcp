import os
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from telegram_mcp.adapters.cli import configure
from telegram_mcp.adapters.config import Settings, load_settings, save_settings
from telegram_mcp.adapters.cursors import CursorStore
from telegram_mcp.domain.models import ToolError
from tests.fakes import FakeBackend, FakeClock


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


@pytest.mark.parametrize(
    "setting", ["allow_public_channel_reads", "allow_public_channel_discovery"]
)
@pytest.mark.parametrize(
    ("initial", "answers", "expected"),
    [
        (False, [""], False),
        (True, [""], True),
        (False, ["y"], True),
        (False, [" YES "], True),
        (True, ["n"], False),
        (True, ["no"], False),
        (False, ["maybe", "y"], True),
    ],
)
async def test_configure_public_channel_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    initial: bool,
    answers: list[str],
    expected: bool,
) -> None:
    backend = FakeBackend()
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    reading = setting == "allow_public_channel_reads"
    prompt_answers = [*answers, ""] if reading else ["", *answers]
    replies = iter(["", "", *prompt_answers, "", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    settings = Settings(
        allowed_chat_ids=["1"],
        allow_public_channel_reads=initial if reading else not initial,
        allow_public_channel_discovery=not initial if reading else initial,
    )
    path = tmp_path / "config.toml"

    await configure(settings, path, lambda _: backend)

    loaded = load_settings(path)
    assert loaded.allow_public_channel_reads is (expected if reading else not initial)
    assert loaded.allow_public_channel_discovery is (not initial if reading else expected)
    assert loaded.allowed_chat_ids == ["1"]
    assert backend.closed


def test_cursor_snapshot_expiry_and_memory_limit() -> None:
    clock = FakeClock()
    store = CursorStore(clock, ttl=10, maximum=1, max_bytes=1000)
    value = ["initial"]
    token = store.put("test", "fp", value)
    value.append("changed")
    assert store.get(token, "test", "fp") == ["initial"]
    store.put("test", "fp", [])
    with pytest.raises(ToolError, match="CURSOR_EXPIRED"):
        store.get(token, "test", "fp")
    with pytest.raises(ToolError, match="QUERY_TOO_LARGE"):
        store.put("test", "fp", "x" * 2000)
    token = store.put("test", "fp", [])
    clock.time = float("inf")
    with pytest.raises(ToolError, match="CURSOR_EXPIRED"):
        store.get(token, "test", "fp")
