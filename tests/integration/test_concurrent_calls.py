import asyncio
import sys

import httpx2
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from telegram_mcp.adapters.config import save_settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import endpoint
from tests.support.concurrency import ConcurrentBackend
from tests.support.transport import running_server

# Real transports and subprocesses; excluded from make test-fast.
pytestmark = pytest.mark.slow


async def assert_overlapping_reads(client: Client) -> None:
    async with asyncio.timeout(15):
        results = await asyncio.gather(
            *(
                client.call_tool("get_history", {"chat_id": cid, "limit": 1})
                for cid in ("1", "2", "3")
            )
        )
        for cid, result in zip(("1", "2", "3"), results, strict=True):
            assert not result.is_error and result.structured_content is not None
            assert result.structured_content["messages"][0]["message_ref"] == f"{cid}:3"
            token = result.structured_content["next_cursor"]
            assert token
            # Concurrent replays must clone the same state, not consume a shared buffer.
            continuations = await asyncio.gather(
                *(
                    client.call_tool("get_history", {"chat_id": cid, "limit": 1, "cursor": token})
                    for _ in range(2)
                )
            )
            for continued in continuations:
                assert not continued.is_error and continued.structured_content is not None
                assert continued.structured_content["messages"][0]["message_ref"] == f"{cid}:2"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_admits_overlapping_histories(tmp_path, mode):
    script = (
        "from pathlib import Path; import sys; "
        "from tests.support.concurrency import concurrent_stdio_service; "
        "from telegram_mcp.adapters.mcp import create_server; "
        "create_server(concurrent_stdio_service(Path(sys.argv[1]))).run()"
    )
    command = StdioServerParameters(command=sys.executable, args=["-c", script, str(tmp_path)])
    async with Client(command, mode=mode, cache=None) as client:
        await assert_overlapping_reads(client)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("transport", ["http", "connect"])
async def test_shared_transports_admit_overlapping_histories(tmp_path, mode, transport):
    async with running_server(tmp_path, ConcurrentBackend()) as (settings, backend):
        if transport == "http":
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {load_token(settings.http_token_path)}"},
                trust_env=False,
            ) as http:
                async with Client(
                    streamable_http_client(endpoint(settings), http_client=http),
                    mode=mode,
                    cache=None,
                ) as client:
                    await assert_overlapping_reads(client)
        else:
            config = tmp_path / "config.toml"
            save_settings(settings, config)
            command = StdioServerParameters(
                command=sys.executable,
                args=["-m", "telegram_mcp.cli", "--config", str(config), "connect"],
            )
            async with Client(command, mode=mode, cache=None) as client:
                await assert_overlapping_reads(client)
        assert set(backend.fetches) == {"1", "2", "3"}
