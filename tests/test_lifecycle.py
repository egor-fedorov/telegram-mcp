import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr
from telethon import errors, functions

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.models import ToolError
from tests.test_catalog_refresh import pinned_page


def settings_for_session(tmp_path: Path) -> Settings:
    session = tmp_path / "test.session"
    session.touch()
    return Settings(api_id=123, api_hash=SecretStr("fake"), session_path=session)


def assert_session_released(settings: Settings) -> None:
    lease = SessionLease(settings.session_path)
    lease.acquire()
    lease.close()


async def test_missing_session_does_not_create_one_or_attempt_login(tmp_path: Path) -> None:
    session = tmp_path / "missing.session"
    runtime = TelegramRuntime(
        Settings(api_id=123, api_hash=SecretStr("fake"), session_path=session)
    )
    status = await runtime.status()
    assert status["authorized"] is False
    assert not session.exists()
    assert not (tmp_path / "missing.session.lock").exists()


async def test_active_session_lock_prevents_second_client(tmp_path: Path) -> None:
    settings = settings_for_session(tmp_path)
    lease = SessionLease(settings.session_path)
    lease.acquire()
    runtime = TelegramRuntime(settings)
    try:
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            await runtime.ensure()
    finally:
        lease.close()


async def test_connection_error_message_does_not_echo_credentials() -> None:
    runtime = TelegramRuntime(Settings())
    request = AsyncMock(side_effect=ConnectionError("sensitive-value"))
    with pytest.raises(ToolError, match="CONNECTION_ERROR") as caught:
        await runtime.call(request())
    assert "sensitive-value" not in str(caught.value)


async def test_constructor_failure_releases_lock(tmp_path: Path) -> None:
    settings = settings_for_session(tmp_path)
    runtime = TelegramRuntime(
        settings, client_factory=Mock(side_effect=sqlite3.DatabaseError("secret"))
    )
    with pytest.raises(ToolError, match="SESSION_ERROR") as caught:
        await runtime.ensure()
    assert "secret" not in str(caught.value)
    assert not runtime.lease.held
    assert_session_released(settings)


async def test_disconnect_failure_still_releases_lock(tmp_path: Path) -> None:
    settings = settings_for_session(tmp_path)
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    runtime = TelegramRuntime(settings, client_factory=Mock(return_value=client))
    await runtime.ensure()
    client.disconnect.side_effect = ConnectionError()
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await runtime.close()
    assert runtime.client is None and not runtime.lease.held
    assert_session_released(settings)


async def test_connect_and_status_probe_authorization_without_cached_helper(tmp_path: Path) -> None:
    settings = settings_for_session(tmp_path)
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    constructor = Mock(return_value=client)
    runtime = TelegramRuntime(settings, client_factory=constructor)
    try:
        assert await runtime.status() == {"authorized": True, "connected": True}
        assert client.await_count == 1
        assert client.await_args is not None
        assert isinstance(client.await_args.args[0], functions.updates.GetStateRequest)
        client.is_user_authorized.assert_not_called()
        options = constructor.call_args.kwargs
        assert options["raise_last_call_error"] is True
        assert options["request_retries"] == options["connection_retries"] == 1
        assert options["flood_sleep_threshold"] == 0
        client.side_effect = errors.SessionRevokedError(None)
        status = await runtime.status()
        assert status["authorized"] is False and status["connected"] is True
        error = status["error"]
        assert isinstance(error, dict) and error["code"] == "AUTH_REQUIRED"
        assert client.await_count == 2
    finally:
        await runtime.close()


@pytest.mark.parametrize("connected", [True, False])
async def test_status_connection_failure_is_unknown_authorization(connected: bool) -> None:
    client = AsyncMock(side_effect=ConnectionError("sensitive-value"))
    client.is_connected = Mock(return_value=connected)
    runtime = TelegramRuntime(Settings(), client=client)
    status = await runtime.status()
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
    tmp_path: Path, error: Exception, authorized: bool | None, code: str
) -> None:
    settings = settings_for_session(tmp_path)
    client = AsyncMock(side_effect=error)
    runtime = TelegramRuntime(settings, client_factory=Mock(return_value=client))
    status = await runtime.status()
    assert status["authorized"] is authorized and status["connected"] is False
    status_error = status["error"]
    assert isinstance(status_error, dict) and status_error["code"] == code
    assert runtime.client is None and not runtime.lease.held
    client.disconnect.assert_awaited_once()
    assert_session_released(settings)


async def test_concurrent_reads_share_one_connected_client(tmp_path: Path) -> None:
    settings = settings_for_session(tmp_path)
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    constructor = Mock(return_value=client)
    runtime = TelegramRuntime(settings, client_factory=constructor)
    try:
        first, second = await asyncio.gather(runtime.ensure(), runtime.ensure())
        assert first is second is client
        constructor.assert_called_once()
        client.connect.assert_awaited_once()
        client.assert_awaited_once()
    finally:
        await runtime.close()
    assert_session_released(settings)


async def test_cancelled_connection_releases_session_lease(tmp_path: Path) -> None:
    settings = settings_for_session(tmp_path)
    started = asyncio.Event()

    async def connect() -> None:
        started.set()
        await asyncio.Event().wait()

    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.connect.side_effect = connect
    runtime = TelegramRuntime(settings, client_factory=Mock(return_value=client))
    pending = asyncio.create_task(runtime.ensure())
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert runtime.client is None and not runtime.lease.held
    client.disconnect.assert_awaited_once()
    assert_session_released(settings)


async def test_catalog_message_and_forum_gateways_share_one_runtime(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from telethon import types

    from telegram_mcp.adapters.telegram import TelegramBackend
    from telegram_mcp.application.ports import TopicOffset
    from telegram_mcp.application.requests import HistoryRead
    from tests.fakes import BASE

    settings = settings_for_session(tmp_path)
    entity = types.Channel(
        id=123,
        title="Forum",
        date=BASE,
        photo=types.ChatPhotoEmpty(),
        access_hash=456,
        forum=True,
        megagroup=True,
    )
    dialog = types.Dialog(
        peer=types.PeerChannel(123),
        top_message=1,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        notify_settings=types.PeerNotifySettings(),
    )

    async def respond(request: object) -> object:
        if isinstance(request, functions.updates.GetStateRequest):
            return None
        if isinstance(request, functions.messages.GetPinnedDialogsRequest):
            return pinned_page([])
        if isinstance(request, functions.messages.GetDialogsRequest):
            return types.messages.Dialogs(dialogs=[dialog], messages=[], chats=[entity], users=[])
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=[])
        assert isinstance(request, functions.messages.GetForumTopicsRequest)
        return types.messages.ForumTopics(
            count=0, topics=[], messages=[], chats=[], users=[], pts=1
        )

    client = AsyncMock(side_effect=respond)
    client.is_connected = Mock(return_value=True)
    client.get_messages.return_value = []
    client.get_input_entity.return_value = types.InputPeerChannel(123, 456)
    constructor = Mock(return_value=client)
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client_factory=constructor)
    )
    try:
        chats, _ = await backend.catalog()
        assert len(chats) == 1 and chats[0].is_forum
        assert (await backend.read(chats[0].id, HistoryRead())).messages == []
        assert (await backend.fetch_topics(chats[0].id, offset=TopicOffset())).topics == []
        constructor.assert_called_once()
        client.connect.assert_awaited_once()
        client.get_entity.assert_not_called()
        client.get_messages.assert_awaited_once()
        client.get_input_entity.assert_awaited_once()
        assert client.await_count == 6
    finally:
        await backend.close()
    client.disconnect.assert_awaited_once()
    assert_session_released(settings)
