"""Maintenance is local, explicit, and excluded while the account server runs."""

import asyncio
import json
from unittest.mock import Mock

import pytest

from telegram_mcp.adapters.cli import CliDependencies, main
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.outbound_store import OutboundStore
from telegram_mcp.adapters.session import SessionLease
from tests.support.fakes import BASE


def invoke(monkeypatch, path, args):
    settings = Settings(session_path=path / "account.session")
    monkeypatch.setattr("telegram_mcp.adapters.cli.load_settings", lambda _: settings)
    monkeypatch.setattr("sys.argv", ["telegram-mcp", *args])
    unused = Mock(side_effect=AssertionError("Maintenance must not contact Telegram"))
    main(CliDependencies(unused, unused, unused, unused, unused))


def test_files_cleanup_previews_then_removes_without_a_telegram_account(
    tmp_path, monkeypatch, capsys
):
    store = OutboundStore(tmp_path / "account.session")
    file = asyncio.run(store.register("101", "cv.pdf", "application/pdf", b"CV", BASE))
    invoke(monkeypatch, tmp_path, ["files", "remove", "--file-id", file.file_id])
    assert json.loads(capsys.readouterr().out)["applied"] is False
    invoke(monkeypatch, tmp_path, ["files", "list"])
    assert json.loads(capsys.readouterr().out)["files"][0]["file_id"] == file.file_id
    invoke(monkeypatch, tmp_path, ["files", "remove", "--file-id", file.file_id, "--apply"])
    assert json.loads(capsys.readouterr().out)["applied"] is True
    invoke(monkeypatch, tmp_path, ["files", "list"])
    assert json.loads(capsys.readouterr().out)["files"] == []


@pytest.mark.parametrize(
    "args",
    [
        ["files", "remove"],
        ["files", "list", "--apply"],
        ["operations", "remove"],
        ["operations", "list", "--file-id", "file"],
        ["files", "list", "--all"],
    ],
)
def test_invalid_maintenance_selectors_are_rejected(tmp_path, monkeypatch, args):
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, tmp_path, args)
    assert error.value.code == 2


def test_maintenance_refuses_an_active_server(tmp_path, monkeypatch, capsys):
    lease = SessionLease(tmp_path / "account.session")
    lease.acquire()
    try:
        with pytest.raises(SystemExit) as error:
            invoke(monkeypatch, tmp_path, ["operations", "prune-payloads", "--apply"])
        assert error.value.code == 1 and "SESSION_IN_USE" in capsys.readouterr().err
    finally:
        lease.close()
