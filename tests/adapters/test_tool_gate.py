import asyncio
import json

import pytest
from mcp.client import Client
from mcp_types import TextContent

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import FakeBackend


async def test_gate_busy_result_and_cancellation_do_not_block_later_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = ToolCallGate(timeout_seconds=0.01, capacity=1)
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
        assert issue["code"] == "SERVER_BUSY" and issue["retry_after_seconds"] == 5
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert not (await client.call_tool("get_status", {})).is_error
        assert backend.backend_calls == ["status"]


async def test_cancelled_gate_waiter_does_not_execute_or_release_active_call() -> None:
    gate = ToolCallGate(capacity=1)
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


async def test_default_gate_runs_four_calls_and_bounds_fifo_waiters():
    gate = ToolCallGate(max_waiters=2)
    release = asyncio.Event()
    started = []

    async def operation(index):
        started.append(index)
        await release.wait()
        return index

    tasks = [asyncio.create_task(gate.run(lambda i=i: operation(i))) for i in range(6)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert started == [0, 1, 2, 3]
    with pytest.raises(ToolError, match="SERVER_BUSY") as caught:
        await gate.run(lambda: operation(6))
    assert caught.value.retry_after == 5
    release.set()
    assert await asyncio.gather(*tasks) == list(range(6))
    assert started == list(range(6))
    await gate.close()


async def test_gate_cancellation_after_slot_transfer_does_not_leak_capacity(monkeypatch):
    gate = ToolCallGate(capacity=1)
    entered, release = asyncio.Event(), asyncio.Event()
    ran = []

    async def active():
        entered.set()
        await release.wait()

    async def queued():
        ran.append(True)

    first = asyncio.create_task(gate.run(active))
    await entered.wait()
    waiting = asyncio.create_task(gate.run(queued))
    await asyncio.sleep(0)
    original = gate._release

    def transfer_then_cancel():
        original()
        monkeypatch.setattr(gate, "_release", original)
        waiting.cancel()

    monkeypatch.setattr(gate, "_release", transfer_then_cancel)
    release.set()
    await first
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not ran
    await asyncio.wait_for(gate.run(queued), 1)
    assert ran == [True]
    await gate.close()


async def test_gate_shutdown_cancels_active_calls_and_rejects_waiters():
    gate = ToolCallGate(capacity=1)
    entered = asyncio.Event()
    ran = []

    async def active():
        entered.set()
        await asyncio.Event().wait()

    async def queued():
        ran.append(True)

    first = asyncio.create_task(gate.run(active))
    await entered.wait()
    waiting = asyncio.create_task(gate.run(queued))
    await asyncio.sleep(0)
    await gate.close()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(ToolError, match="SERVER_CLOSED"):
        await waiting
    with pytest.raises(ToolError, match="SERVER_CLOSED"):
        await gate.run(queued)
    assert not ran
