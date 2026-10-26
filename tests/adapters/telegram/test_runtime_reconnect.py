"""Recovery never creates competing session owners or replays an application RPC."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr
from telethon import errors, functions

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.telegram import TelegramBackend
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import FakeClock


def runtime_with_client(tmp_path: Path):
    path = tmp_path / "account.session"
    path.touch()
    clock = FakeClock()
    client = AsyncMock()
    client.connected = False
    client.is_connected = Mock(side_effect=lambda: client.connected)

    async def connect() -> None:
        client.connected = True

    async def disconnect() -> None:
        client.connected = False

    client.connect.side_effect = connect
    client.disconnect.side_effect = disconnect
    factory = Mock(return_value=client)
    settings = Settings(api_id=123, api_hash=SecretStr("synthetic"), session_path=path)
    return TelegramRuntime(settings, client_factory=factory, clock=clock), client, factory, clock


async def test_disconnected_owned_client_reconnects_once_and_rechecks_auth(tmp_path: Path) -> None:
    runtime, client, factory, _ = runtime_with_client(tmp_path)
    await runtime.ensure()
    generation = runtime.generation
    client.connected = False
    first, second = await asyncio.gather(runtime.ensure(), runtime.ensure())
    assert first is second is client
    factory.assert_called_once()
    assert client.connect.await_count == client.await_count == 2
    assert runtime.generation > generation and runtime.lease.held
    with pytest.raises(ToolError, match="SESSION_IN_USE"):
        SessionLease(runtime.settings.session_path).acquire()
    await runtime.close()


async def test_failed_reconnect_holds_lease_and_obeys_five_second_cooldown(tmp_path: Path) -> None:
    runtime, client, factory, clock = runtime_with_client(tmp_path)
    await runtime.ensure()
    client.connected = False
    client.connect.side_effect = ConnectionError("synthetic-private-value")
    with pytest.raises(ToolError, match="CONNECTION_ERROR") as caught:
        await runtime.ensure()
    assert caught.value.retry_after == 5
    clock.time += 4.1
    with pytest.raises(ToolError, match="CONNECTION_ERROR") as caught:
        await runtime.ensure()
    assert caught.value.retry_after == 1
    assert client.connect.await_count == 2 and runtime.lease.held
    assert "synthetic-private-value" not in str(caught.value)

    async def reconnect() -> None:
        client.connected = True

    client.connect.side_effect = reconnect
    clock.time += 0.9
    assert await runtime.ensure() is client
    assert client.connect.await_count == 3
    factory.assert_called_once()
    await runtime.close()


@pytest.mark.parametrize(
    "wait_type",
    [errors.FloodWaitError, errors.FloodPremiumWaitError, errors.FloodTestPhoneWaitError],
)
async def test_reconnect_probe_flood_wait_does_not_reconnect_or_probe_early(
    tmp_path: Path,
    wait_type: type[
        errors.FloodWaitError | errors.FloodPremiumWaitError | errors.FloodTestPhoneWaitError
    ],
) -> None:
    runtime, client, _, clock = runtime_with_client(tmp_path)
    await runtime.ensure()
    client.connected = False
    client.side_effect = wait_type(None, capture=17)
    status = await runtime.status()
    assert status["authorized"] is None and status["connected"] is True
    assert client.connect.await_count == 2 and client.await_count == 2
    clock.time += 16
    status = await runtime.status()
    assert status["error"]["retry_after_seconds"] == 1
    assert client.await_count == 2
    client.side_effect = None
    clock.time += 1
    assert await runtime.status() == {"authorized": True, "connected": True}
    assert client.connect.await_count == 2 and client.await_count == 3
    await runtime.close()


async def test_status_after_reconnect_performs_one_fresh_probe(tmp_path: Path) -> None:
    runtime, client, _, _ = runtime_with_client(tmp_path)
    await runtime.ensure()
    client.connected = False
    assert await runtime.status() == {"authorized": True, "connected": True}
    assert client.await_count == 2
    client.side_effect = errors.SessionRevokedError(None)
    assert (await runtime.status())["authorized"] is False
    assert runtime.auth_generation == 1
    assert client.connect.await_count == 2
    await runtime.close()


async def test_cancelled_reconnect_retains_ownership_until_shutdown(tmp_path: Path) -> None:
    runtime, client, _, _ = runtime_with_client(tmp_path)
    await runtime.ensure()
    client.connected = False
    entered = asyncio.Event()

    async def blocked_connect() -> None:
        entered.set()
        await asyncio.Event().wait()

    client.connect.side_effect = blocked_connect
    pending = asyncio.create_task(runtime.ensure())
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert runtime.client is client and runtime.lease.held
    await runtime.close()
    assert not runtime.lease.held


async def test_shutdown_waits_for_connection_then_prevents_runtime_resurrection(
    tmp_path: Path,
) -> None:
    runtime, client, factory, _ = runtime_with_client(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_connect() -> None:
        entered.set()
        await release.wait()
        client.connected = True

    client.connect.side_effect = blocked_connect
    pending = asyncio.create_task(runtime.ensure())
    await entered.wait()
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    assert not closing.done() and runtime.lease.held
    release.set()
    await asyncio.gather(pending, closing)
    assert runtime.client is None and not runtime.lease.held
    with pytest.raises(ToolError, match="closed"):
        await runtime.ensure()
    factory.assert_called_once()
    client.disconnect.assert_awaited_once()


async def test_shutdown_waits_for_fresh_authorization_probe(tmp_path: Path) -> None:
    runtime, client, _, _ = runtime_with_client(tmp_path)
    await runtime.ensure()
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(request):
        entered.set()
        await release.wait()

    client.side_effect = probe
    pending = asyncio.create_task(runtime.verify_authorization())
    await entered.wait()
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    assert not closing.done()
    client.disconnect.assert_not_called()
    release.set()
    await asyncio.gather(pending, closing)
    assert runtime.client is None and not runtime.lease.held


async def test_older_successful_probe_cannot_override_concurrent_authorization_loss(
    tmp_path: Path,
) -> None:
    runtime, client, _, _ = runtime_with_client(tmp_path)
    await runtime.ensure()
    entered, release = asyncio.Event(), asyncio.Event()

    async def stale_probe(request):
        entered.set()
        await release.wait()

    client.side_effect = stale_probe
    pending = asyncio.create_task(runtime.status())
    await entered.wait()
    try:
        with pytest.raises(ToolError, match="AUTH_REQUIRED"):
            await runtime.call(AsyncMock(side_effect=errors.SessionRevokedError(None))())
    finally:
        release.set()
    status = await pending
    assert status["authorized"] is False and status["connected"] is True
    assert runtime.lease.held
    client.side_effect = None
    await runtime.ensure()
    assert client.await_count == 3
    await runtime.close()


async def test_flood_wait_is_structured_and_does_not_sleep() -> None:
    backend = TelegramBackend(Settings())
    request = functions.messages.GetDialogFiltersRequest()

    async def fail() -> None:
        raise errors.FloodWaitError(request, capture=17)

    with pytest.raises(ToolError) as caught:
        await backend.runtime.call(fail())
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.retry_after == 17


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (errors.RpcCallFailError(None), "TELEGRAM_UNAVAILABLE", True),
        (errors.TimedOutError(None, "timeout"), "TELEGRAM_UNAVAILABLE", True),
        (ConnectionError("secret"), "CONNECTION_ERROR", True),
        (errors.SessionExpiredError(None), "AUTH_REQUIRED", False),
        (errors.ChannelPrivateError(None), "TELEGRAM_ERROR", False),
    ],
)
async def test_error_retryability_is_explicit(error: Exception, code: str, retryable: bool) -> None:
    backend = TelegramBackend(Settings())
    with pytest.raises(ToolError) as caught:
        await backend.runtime.call(AsyncMock(side_effect=error)())
    assert caught.value.code == code and caught.value.retryable is retryable
    issue = caught.value.issue(input_index=3)
    assert issue.input_index == 3 and issue.message_ref is None
