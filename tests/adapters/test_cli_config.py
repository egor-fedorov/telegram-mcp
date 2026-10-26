"""Actionable local configuration errors and CLI dispatch without account access."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from telegram_mcp.adapters.cli import CliDependencies, configure, login, main
from telegram_mcp.adapters.config import Settings, load_settings, save_settings
from telegram_mcp.adapters.version import get_version
from telegram_mcp.domain.models import Folder, ToolError
from tests.support.fakes import FakeBackend


@pytest.fixture(autouse=True)
def clear_telegram_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("content", "key"),
    [
        ('api_id = "sensitive-value"', "api_id"),
        ("rpc_timeout_seconds = 0", "rpc_timeout_seconds"),
        ('allowed_folder_ids = ["sensitive-value"]', "allowed_folder_ids.0"),
        ('api_hash = ["sensitive-value"]', "api_hash"),
        ('api_hash = ""', "api_hash"),
        ('unknown_setting = "sensitive-value"', "unknown_setting"),
        ("http_port = 65536", "http_port"),
        ("http_port = true", "http_port"),
    ],
)
def test_validation_names_source_and_key_without_values(
    tmp_path: Path, content: str, key: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ToolError, match="INVALID_CONFIGURATION") as caught:
        load_settings(path)
    message = str(caught.value)
    assert str(path) in message and key in message
    assert "sensitive-value" not in message and "input_value" not in message


def test_environment_error_names_override_instead_of_blame_on_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("api_id = 123\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_API_ID", "sensitive-value")
    with pytest.raises(ToolError, match="TELEGRAM_API_ID: api_id") as caught:
        load_settings(path)
    assert "sensitive-value" not in str(caught.value)


def test_toml_error_preserves_position_without_source_line(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('api_hash = "sensitive-value"\nallowed_chat_ids = ]\n', encoding="utf-8")
    with pytest.raises(ToolError, match="invalid TOML syntax at line 2") as caught:
        load_settings(path)
    assert "sensitive-value" not in str(caught.value)


def test_invalid_utf8_has_its_own_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"api_hash = '\xff'\n")
    with pytest.raises(ToolError, match="configuration must use UTF-8"):
        load_settings(path)


def test_missing_config_remains_valid_for_first_login(tmp_path: Path) -> None:
    path = tmp_path / "not-created.toml"
    assert load_settings(path).api_id is None
    assert not path.exists()


def test_io_error_identifies_operation_without_raw_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=PermissionError("sensitive-value")))
    with pytest.raises(ToolError, match="Cannot read configuration") as caught:
        load_settings(path)
    assert str(path) in str(caught.value) and "permission denied" in str(caught.value)
    assert "sensitive-value" not in str(caught.value)


def test_failed_atomic_replace_preserves_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = "allow_all_chats = true\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(os, "replace", Mock(side_effect=PermissionError("sensitive-value")))
    with pytest.raises(ToolError, match="Cannot write configuration") as caught:
        save_settings(Settings(), path)
    assert "sensitive-value" not in str(caught.value)
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [path]


def test_config_roundtrip_uses_utf8_and_expands_shared_server_paths(tmp_path: Path) -> None:
    path = tmp_path / "настройки.toml"
    settings = Settings(
        session_path=tmp_path / "сессия.session",
        http_token_path=tmp_path / "токен.token",
        http_port=18765,
    )
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.http_token_path == settings.http_token_path
    assert loaded.session_path == settings.session_path
    assert loaded.http_port == 18765
    assert "токен" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("initial", "answer", "expected"), [(True, "", True), (True, "n", False), (False, "y", True)]
)
async def test_configure_all_chats_is_explicit_and_preserves_input_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: bool, answer: str, expected: bool
) -> None:
    settings = Settings(allow_all_chats=initial, allowed_chat_ids=["1"])
    backend = FakeBackend()
    replies = iter(["", "", "", "", answer, ""])
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    path = tmp_path / "config.toml"
    await configure(settings, path, lambda _: backend)
    assert load_settings(path).allow_all_chats is expected
    assert settings.allow_all_chats is initial
    assert settings.allowed_chat_ids == ["1"]
    assert backend.closed


async def test_interrupted_configure_does_not_save_or_mutate_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(allowed_folder_ids=[1], allow_all_chats=True)
    backend = FakeBackend(folders=[Folder(id=2, title="Available", position=0)])
    path = tmp_path / "config.toml"
    save_settings(settings, path)
    original = path.read_bytes()
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", Mock(side_effect=["2", EOFError()]))
    with pytest.raises(EOFError):
        await configure(settings, path, lambda _: backend)
    assert settings.allowed_folder_ids == [1] and settings.allow_all_chats
    assert path.read_bytes() == original and backend.closed


@pytest.mark.parametrize(
    ("replies", "field"),
    [(["invalid"], "allowed_folder_ids"), (["", "invalid"], "allowed_chat_ids")],
)
async def test_configure_invalid_ids_name_the_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replies: list[str], field: str
) -> None:
    backend = FakeBackend()
    answers = iter(replies)
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    with pytest.raises(ToolError, match=field):
        await configure(Settings(), tmp_path / "config.toml", lambda _: backend)
    assert backend.closed


async def test_bad_login_id_does_not_attempt_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authenticate = AsyncMock()
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "sensitive-value")
    with pytest.raises(ToolError, match="api_id: enter a positive integer"):
        await login(Settings(), tmp_path / "config.toml", authenticate)
    authenticate.assert_not_called()


async def test_empty_login_hash_is_diagnosed_before_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authenticate = AsyncMock()
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("telegram_mcp.adapters.cli.getpass.getpass", lambda _: "")
    with pytest.raises(ToolError, match="Login input: api_hash"):
        await login(Settings(api_id=123), tmp_path / "config.toml", authenticate)
    authenticate.assert_not_called()


def dependencies() -> CliDependencies:
    return CliDependencies(Mock(), Mock(), AsyncMock(), Mock(), AsyncMock())


@pytest.mark.parametrize("executable", ["telegram-mcp", "telegram-mcp.exe"])
def test_version_skips_configuration_and_backends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], executable: str
) -> None:
    dependencies_ = dependencies()
    load = Mock(side_effect=AssertionError("must not read configuration"))
    monkeypatch.setattr("telegram_mcp.adapters.cli.load_settings", load)
    monkeypatch.setattr("sys.argv", [executable, "--version"])
    with pytest.raises(SystemExit) as caught:
        main(dependencies_)
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"telegram-mcp {get_version()}"
    load.assert_not_called()


def test_connect_dispatch_never_constructs_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependencies_ = dependencies()
    monkeypatch.setattr(
        "sys.argv", ["telegram-mcp", "--config", str(tmp_path / "config.toml"), "connect"]
    )
    main(dependencies_)
    assert isinstance(dependencies_.connect, AsyncMock)
    dependencies_.connect.assert_awaited_once()
    assert isinstance(dependencies_.backend_factory, Mock)
    dependencies_.backend_factory.assert_not_called()


def test_http_dispatch_uses_shared_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dependencies_ = dependencies()
    monkeypatch.setattr(
        "sys.argv",
        ["telegram-mcp", "--config", str(tmp_path / "config.toml"), "serve", "--transport", "http"],
    )
    main(dependencies_)
    assert isinstance(dependencies_.serve_http, Mock)
    dependencies_.serve_http.assert_called_once()


def test_cli_configuration_failure_is_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('api_id = "sensitive-value"\n', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["telegram-mcp", "--config", str(path), "serve"])
    with pytest.raises(SystemExit) as caught:
        main(dependencies())
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert output.out == "" and "api_id" in output.err
    assert "sensitive-value" not in output.err


@pytest.mark.parametrize(
    ("initial", "answer", "expected"),
    [(False, "", False), (True, "", True), (False, "y", True), (True, "n", False)],
)
async def test_configure_download_permission_is_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: bool, answer: str, expected: bool
) -> None:
    settings = Settings(allow_attachment_downloads=initial, allowed_chat_ids=["1"])
    replies = iter(["", "", "", "", "", answer])
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    path = tmp_path / "config.toml"
    await configure(settings, path, lambda _: FakeBackend())
    loaded = load_settings(path)
    assert loaded.allow_attachment_downloads is expected
    assert not loaded.allow_all_chats and not loaded.allow_public_channel_reads
    assert loaded.allowed_chat_ids == ["1"]
    assert settings.allow_attachment_downloads is initial


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
