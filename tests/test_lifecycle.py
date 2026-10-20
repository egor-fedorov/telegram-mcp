import fcntl
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr
from telethon import errors, functions

from telegram_mcp.config import Settings
from telegram_mcp.models import ToolError
from telegram_mcp.telegram import TelegramBackend


async def test_missing_session_does_not_create_one_or_attempt_login(tmp_path: Path) -> None:
    session = tmp_path / "missing.session"
    backend = TelegramBackend(
        Settings(api_id=123, api_hash=SecretStr("fake"), session_path=session)
    )
    status = await backend.status()
    assert status["authorized"] is False
    assert not session.exists()
    assert not (tmp_path / "missing.session.lock").exists()


async def test_active_session_lock_prevents_second_client(tmp_path: Path) -> None:
    session = tmp_path / "test.session"
    session.touch()
    fd = os.open(str(session) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    backend = TelegramBackend(
        Settings(api_id=123, api_hash=SecretStr("fake"), session_path=session)
    )
    try:
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            await backend.ensure()
    finally:
        os.close(fd)


async def test_connection_error_message_does_not_echo_credentials() -> None:
    backend = TelegramBackend(Settings())
    request = AsyncMock(side_effect=ConnectionError("sensitive-value"))
    with pytest.raises(ToolError, match="CONNECTION_ERROR") as caught:
        await backend.call(request())
    assert "sensitive-value" not in str(caught.value)


async def test_constructor_failure_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "test.session"
    session.touch()
    backend = TelegramBackend(
        Settings(api_id=123, api_hash=SecretStr("fake"), session_path=session)
    )
    monkeypatch.setattr(
        "telegram_mcp.telegram.TelegramClient", Mock(side_effect=sqlite3.DatabaseError("secret"))
    )
    with pytest.raises(ToolError, match="SESSION_ERROR") as caught:
        await backend.ensure()
    assert "secret" not in str(caught.value)
    assert backend.lock_fd is None
    fd = os.open(str(session) + ".lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


async def test_disconnect_failure_still_releases_lock(tmp_path: Path) -> None:
    backend = TelegramBackend(Settings())
    backend.lock_fd = os.open(tmp_path / "session.lock", os.O_CREAT | os.O_RDWR, 0o600)
    backend.client = Mock(disconnect=AsyncMock(side_effect=ConnectionError()))
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await backend.close()
    assert backend.client is None and backend.lock_fd is None


async def test_connect_and_status_probe_authorization_without_cached_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "test.session"
    session.touch()
    backend = TelegramBackend(
        Settings(api_id=123, api_hash=SecretStr("fake"), session_path=session)
    )
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    constructor = Mock(return_value=client)
    monkeypatch.setattr("telegram_mcp.telegram.TelegramClient", constructor)
    try:
        assert await backend.status() == {"authorized": True, "connected": True}
        assert client.await_count == 1
        assert client.await_args is not None
        assert isinstance(client.await_args.args[0], functions.updates.GetStateRequest)
        client.is_user_authorized.assert_not_called()
        settings = constructor.call_args.kwargs
        assert settings["raise_last_call_error"] is True
        assert settings["request_retries"] == settings["connection_retries"] == 1
        assert settings["flood_sleep_threshold"] == 0
        client.side_effect = errors.SessionRevokedError(None)
        status = await backend.status()
        assert status["authorized"] is False and status["connected"] is True
        error = status["error"]
        assert isinstance(error, dict) and error["code"] == "AUTH_REQUIRED"
        assert client.await_count == 2
    finally:
        await backend.close()


@pytest.mark.parametrize("connected", [True, False])
async def test_status_connection_failure_is_unknown_authorization(connected: bool) -> None:
    backend = TelegramBackend(Settings())
    client = AsyncMock(side_effect=ConnectionError("sensitive-value"))
    client.is_connected = Mock(return_value=connected)
    backend.client = client
    status = await backend.status()
    assert status["authorized"] is None and status["connected"] is connected
    error = status["error"]
    assert isinstance(error, dict) and error["code"] == "CONNECTION_ERROR"
    assert "sensitive-value" not in str(status)


@pytest.mark.parametrize(
    ("error", "authorized", "code"),
    [
        (errors.AuthKeyUnregisteredError(None), False, "AUTH_REQUIRED"),
        (ConnectionError("secret"), None, "CONNECTION_ERROR"),
        (errors.RpcCallFailError(None), None, "TELEGRAM_UNAVAILABLE"),
    ],
)
async def test_failed_initial_auth_probe_releases_session_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    authorized: bool | None,
    code: str,
) -> None:
    session = tmp_path / "test.session"
    session.touch()
    backend = TelegramBackend(
        Settings(api_id=123, api_hash=SecretStr("fake"), session_path=session)
    )
    client = AsyncMock(side_effect=error)
    monkeypatch.setattr("telegram_mcp.telegram.TelegramClient", Mock(return_value=client))
    status = await backend.status()
    assert status["authorized"] is authorized and status["connected"] is False
    status_error = status["error"]
    assert isinstance(status_error, dict) and status_error["code"] == code
    assert backend.client is None and backend.lock_fd is None
    client.disconnect.assert_awaited_once()
    fd = os.open(str(session) + ".lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
