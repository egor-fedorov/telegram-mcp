import json
from unittest.mock import Mock

import pytest

from telegram_mcp.adapters.cli import CliDependencies, main
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.session import SessionLease


def invoke(monkeypatch, tmp_path, args):
    settings = Settings(session_path=tmp_path / "account.session")
    monkeypatch.setattr("telegram_mcp.adapters.cli.load_settings", lambda _: settings)
    monkeypatch.setattr("telegram_mcp.adapters.checkpoints.user_data_path", lambda _: tmp_path)
    monkeypatch.setattr("sys.argv", ["telegram-mcp", *args])
    unused = Mock(side_effect=AssertionError("Local maintenance must not create a Telegram client"))
    main(CliDependencies(unused, unused, unused, unused, unused))


def test_local_checkpoint_status_and_prune_need_no_account(monkeypatch, tmp_path, capsys):
    invoke(monkeypatch, tmp_path, ["checkpoints", "status"])
    assert json.loads(capsys.readouterr().out)["count"] == 0
    invoke(monkeypatch, tmp_path, ["checkpoints", "prune"])
    assert json.loads(capsys.readouterr().out)["applied"] is False
    invoke(
        monkeypatch, tmp_path, ["checkpoints", "prune", "--all", "--include-protected", "--apply"]
    )
    assert json.loads(capsys.readouterr().out)["applied"] is True


@pytest.mark.parametrize(
    "args",
    [
        ["checkpoints"],
        ["checkpoints", "status", "--apply"],
        ["checkpoints", "prune", "--include-protected"],
        ["checkpoints", "prune", "--all", "--chat-id=1"],
        ["status", "--apply"],
    ],
)
def test_invalid_checkpoint_cli_combinations_are_rejected(monkeypatch, tmp_path, args):
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, tmp_path, args)
    assert error.value.code == 2


def test_checkpoint_cleanup_refuses_an_active_server(monkeypatch, tmp_path, capsys):
    lease = SessionLease(tmp_path / "account.session")
    lease.acquire()
    try:
        with pytest.raises(SystemExit) as error:
            invoke(monkeypatch, tmp_path, ["checkpoints", "prune", "--apply"])
        assert error.value.code == 1
        assert "SESSION_IN_USE" in capsys.readouterr().err
        assert not list(tmp_path.rglob("checkpoints.sqlite3"))
    finally:
        lease.close()
