"""Prompt discovery, rendering, errors and completion are equivalent across transports."""

import asyncio
import sys
from contextlib import asynccontextmanager

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, ListToolsResult, PromptReference

from telegram_mcp.adapters.bridge import create_bridge
from tests.support.fakes import BASE
from tests.support.transport import connection, running_server

PERIOD = {
    "chat_ref": "1",
    "date_from": "2026-01-01",
    "date_to": "2026-01-08",
    "timezone": "Europe/Moscow",
    "focus": "Remote Python vacancies",
}


async def snapshot(client: Client) -> dict:
    await client.call_tool(
        "create_watch", {"name": "jobs", "chat_refs": ["1"], "date_from": BASE.isoformat()}
    )
    prompts = (await client.list_prompts()).prompts
    period = await client.get_prompt("period_digest", PERIOD)
    watch = await client.get_prompt("watch_digest", {"watch_name": "jobs", "save": "false"})
    with pytest.raises(MCPError) as caught:
        await client.get_prompt("period_digest", {"chat_ref": "1", "days": "0"})
    assert caught.value.error.code == INVALID_PARAMS
    completion = await client.complete(
        PromptReference(name="watch_digest"), {"name": "watch_name", "value": "jo"}
    )
    return {
        "prompts": [prompt.model_dump(mode="json", exclude_none=True) for prompt in prompts],
        "period": period.model_dump(mode="json", exclude_none=True)["messages"],
        "watch": watch.model_dump(mode="json", exclude_none=True)["messages"],
        "error": caught.value.error.message,
        "completion": completion.completion.values,
    }


@asynccontextmanager
async def stdio_client(path, mode):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from pathlib import Path; import sys; "
            "from tests.support.incremental import export_stdio_service; "
            "from telegram_mcp.adapters.mcp import create_server; "
            "create_server(export_stdio_service(Path(sys.argv[1]))).run()",
            str(path),
        ],
    )
    async with Client(parameters, mode=mode, cache=None) as client:
        yield client


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_prompts_and_completion_match_across_stdio_http_and_connect(tmp_path, mode):
    async with asyncio.timeout(60):
        stdio_dir = tmp_path / "stdio"
        stdio_dir.mkdir()
        async with stdio_client(stdio_dir, mode) as client:
            expected = await snapshot(client)
        assert expected["completion"] == ["jobs"]
        for kind in ("http", "connect"):
            directory = tmp_path / kind
            directory.mkdir()
            async with running_server(directory) as (settings, _):
                async with connection(kind, directory, mode, "compact", settings) as client:
                    assert await snapshot(client) == expected


async def test_bridge_does_not_advertise_capabilities_the_upstream_lacks():
    async def list_tools(_context, _params):
        return ListToolsResult(tools=[])

    upstream_server = Server("Minimal", on_list_tools=list_tools)
    async with Client(upstream_server) as upstream:
        assert upstream.server_capabilities.prompts is None
        async with Client(create_bridge(upstream)) as client:
            assert client.server_capabilities.prompts is None
            assert client.server_capabilities.completions is None
            with pytest.raises(MCPError):
                await client.list_prompts()
