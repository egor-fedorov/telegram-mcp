"""Local QR rendering and CLI dispatch never expose login tokens to non-TTY output."""

import io
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from telegram_mcp.adapters import cli, qr_code
from telegram_mcp.adapters.config import Settings, load_settings
from telegram_mcp.domain.models import ToolError
from tests.support.qr_auth import TOKEN


class Terminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


def terminal(monkeypatch) -> Terminal:
    output = Terminal()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdout", output)
    return output


def test_real_encoder_renders_a_qr_without_printing_its_url(monkeypatch):
    output = terminal(monkeypatch)
    qr_code.show_qr("tg://login?token=" + TOKEN)
    rendered = output.getvalue()
    assert "Devices" in rendered and "Ctrl+C" in rendered
    assert any(block in rendered for block in "\u2580\u2584\u2588")
    assert TOKEN not in rendered and "tg://login" not in rendered


@pytest.mark.parametrize("redirected", ["stdin", "stdout"])
async def test_qr_login_requires_input_and_output_ttys_before_credentials(
    tmp_path, monkeypatch, redirected
):
    terminal(monkeypatch)
    monkeypatch.setattr(getattr(sys, redirected), "isatty", lambda: False)
    authenticate = AsyncMock()
    with pytest.raises(ToolError, match="INTERACTIVE_LOGIN_REQUIRED"):
        await cli.login(Settings(), tmp_path / "config.toml", authenticate, qr=True)
    authenticate.assert_not_called()
    assert not (tmp_path / "config.toml").exists()


def test_incompatible_encoding_is_rejected_before_a_token_is_requested(monkeypatch):
    class AsciiTerminal(Terminal):
        encoding = "ascii"

    terminal(monkeypatch)
    monkeypatch.setattr(sys, "stdout", AsciiTerminal())
    with pytest.raises(ToolError, match="QR_TERMINAL_UNSUPPORTED"):
        qr_code.require_terminal()


@pytest.mark.parametrize("qr", [False, True])
async def test_login_passes_the_selected_mode_and_preserves_configuration(
    tmp_path: Path, monkeypatch, qr: bool
) -> None:
    terminal(monkeypatch)
    settings = Settings(
        api_id=123,
        api_hash=SecretStr("synthetic"),
        session_path=tmp_path / "test.session",
        allowed_chat_ids=["42"],
        allow_all_chats=True,
    )
    before = settings.model_dump()
    authenticate = AsyncMock()
    path = tmp_path / "config.toml"
    await cli.login(settings, path, authenticate, qr=qr)
    assert authenticate.call_args.kwargs["show_qr"] is (qr_code.show_qr if qr else None)
    assert settings.model_dump() == before and load_settings(path).model_dump() == before


async def test_failed_qr_login_does_not_save_configuration(tmp_path, monkeypatch):
    terminal(monkeypatch)
    settings = Settings(api_id=123, api_hash=SecretStr("synthetic"))
    path = tmp_path / "config.toml"
    authenticate = AsyncMock(side_effect=ToolError("QR_LOGIN_EXPIRED", "Expired"))
    with pytest.raises(ToolError, match="QR_LOGIN_EXPIRED"):
        await cli.login(settings, path, authenticate, qr=True)
    assert not path.exists()


@pytest.mark.parametrize("command", ["serve", "connect", "configure", "status", "catalog"])
def test_qr_flag_is_rejected_for_other_commands(monkeypatch, capsys, command):
    dependencies = cli.CliDependencies(Mock(), Mock(), AsyncMock(), Mock(), AsyncMock())
    load = Mock(side_effect=AssertionError("Must validate flags first"))
    monkeypatch.setattr(cli, "load_settings", load)
    monkeypatch.setattr(sys, "argv", ["telegram-mcp", command, "--qr"])
    with pytest.raises(SystemExit) as caught:
        cli.main(dependencies)
    assert caught.value.code == 2 and "--qr is supported only with login" in capsys.readouterr().err
    load.assert_not_called()


def test_cli_dispatches_login_with_qr(tmp_path, monkeypatch):
    terminal(monkeypatch)
    authenticate = AsyncMock()
    dependencies = cli.CliDependencies(Mock(), Mock(), authenticate, Mock(), AsyncMock())
    settings = Settings(api_id=123, api_hash=SecretStr("synthetic"))
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(
        sys, "argv", ["telegram-mcp", "--config", str(tmp_path / "config.toml"), "login", "--qr"]
    )
    cli.main(dependencies)
    assert authenticate.call_args.kwargs["show_qr"] is qr_code.show_qr
