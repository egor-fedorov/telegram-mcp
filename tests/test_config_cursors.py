import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from telegram_mcp.cli import configure, save_settings
from telegram_mcp.config import Settings, load_settings
from telegram_mcp.cursors import CursorStore
from telegram_mcp.models import ToolError
from tests.fakes import FakeBackend


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
    )
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.credentials() == (123, "example-not-a-real-key")
    assert loaded.allowed_folder_ids == [1]
    assert loaded.allow_public_channel_reads is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "example-not-a-real-key" not in repr(loaded)


def test_public_channels_require_explicit_configuration(tmp_path: Path) -> None:
    assert Settings().allow_public_channel_reads is False
    path = tmp_path / "config.toml"
    path.write_text('allowed_chat_ids = ["1"]\n')
    assert load_settings(path).allow_public_channel_reads is False


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
    initial: bool,
    answers: list[str],
    expected: bool,
) -> None:
    backend = FakeBackend()
    monkeypatch.setattr("telegram_mcp.cli.TelegramBackend", lambda _: backend)
    monkeypatch.setattr("telegram_mcp.cli.sys.stdin.isatty", lambda: True)
    replies = iter(["", "", *answers])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    settings = Settings(allowed_chat_ids=["1"], allow_public_channel_reads=initial)
    path = tmp_path / "config.toml"

    await configure(settings, path)

    loaded = load_settings(path)
    assert loaded.allow_public_channel_reads is expected
    assert loaded.allowed_chat_ids == ["1"]
    assert backend.closed


def test_cursor_snapshot_expiry_and_memory_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = CursorStore(ttl=10, maximum=1, max_bytes=1000)
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
    monkeypatch.setattr("telegram_mcp.cursors.time.monotonic", lambda: float("inf"))
    with pytest.raises(ToolError, match="CURSOR_EXPIRED"):
        store.get(token, "test", "fp")
