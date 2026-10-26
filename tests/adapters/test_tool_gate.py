import asyncio
import json

import pytest
from mcp.client import Client
from mcp_types import TextContent

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.bootstrap import build_service
from tests.support.fakes import FakeBackend


async def test_gate_busy_result_and_cancellation_do_not_block_later_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = ToolCallGate(timeout_seconds=0.01)
    backend = FakeBackend()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = backend.status

    async def blocked_status() -> dict[str, object]:
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(backend, "status", blocked_status)
    server = create_server(build_service(backend, Settings()), gate=gate)
    async with Client(server) as client:
        first = asyncio.create_task(client.call_tool("get_status", {}))
        await entered.wait()
        busy = await client.call_tool("get_status", {})
        assert busy.is_error
        assert isinstance(busy.content[0], TextContent)
        text = busy.content[0].text
        issue = json.loads(text[text.index("{") :])
        assert issue["code"] == "SERVER_BUSY" and issue["retry_after_seconds"] == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert not (await client.call_tool("get_status", {})).is_error
        assert backend.backend_calls == ["status"]


async def test_cancelled_gate_waiter_does_not_execute_or_release_active_call() -> None:
    gate = ToolCallGate()
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def active():
        entered.set()
        await release.wait()
        calls.append("active")

    async def queued():
        calls.append("queued")

    first = asyncio.create_task(gate.run(active))
    await entered.wait()
    waiting = asyncio.create_task(gate.run(queued))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not calls
    release.set()
    await first
    await gate.run(queued)
    assert calls == ["active", "queued"]
