import fcntl
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

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
