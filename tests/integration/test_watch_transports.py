import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from telegram_mcp.adapters.config import save_settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import endpoint
from tests.support.fakes import BASE
from tests.support.transport import running_server


@asynccontextmanager
async def connection(kind, path, mode, schema_mode, settings=None):
    if kind == "stdio":
        script = (
            "from pathlib import Path; import sys; "
            "from tests.support.incremental import export_stdio_service; "
            "from telegram_mcp.adapters.mcp import create_server; "
            "create_server(export_stdio_service(Path(sys.argv[1])), schema_mode=sys.argv[2]).run()"
        )
        parameters = StdioServerParameters(
            command=sys.executable, args=["-c", script, str(path), schema_mode]
        )
        async with Client(parameters, mode=mode, cache=None) as client:
            yield client
    elif kind == "connect":
        assert settings is not None
        config = path / "config.toml"
        save_settings(settings, config)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telegram_mcp.cli", "--config", str(config), "connect"],
        )
        async with Client(parameters, mode=mode, cache=None) as client:
            yield client
    else:
        assert settings is not None
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {load_token(settings.http_token_path)}"},
            trust_env=False,
        ) as http:
            async with Client(
                streamable_http_client(endpoint(settings), http_client=http), mode=mode, cache=None
            ) as client:
                yield client


async def exercise(kind, path, mode, schema_mode, settings=None):
    async with connection(kind, path, mode, schema_mode, settings) as client:
        tools = (await client.list_tools()).tools
        assert all((tool.output_schema is None) == (schema_mode == "compact") for tool in tools)
        created = await client.call_tool(
            "create_watch", {"name": "jobs", "chat_refs": ["1"], "date_from": BASE.isoformat()}
        )
        assert not created.is_error
        first = await client.call_tool("get_watch_changes", {"name": "jobs", "limit": 1})
        assert not first.is_error and first.structured_content
        batch = first.structured_content
        assert [item["message_ref"] for item in batch["changes"]] == ["1:1"]
    # STDIO restarts the owning process; HTTP/connect replace only the client.
    async with connection(kind, path, mode, schema_mode, settings) as client:
        found = await client.call_tool("list_watches", {})
        assert found.structured_content["watches"][0]["pending_batch_id"] == batch["batch_id"]
        replay = await client.call_tool("get_watch_changes", {"name": "jobs"})
        assert replay.structured_content == batch
        ack = await client.call_tool(
            "ack_watch_changes", {"name": "jobs", "batch_id": batch["batch_id"]}
        )
        assert not ack.is_error
        second = await client.call_tool("get_watch_changes", {"name": "jobs", "limit": 1})
        assert second.structured_content["changes"][0]["message_ref"] == "1:2"
        exported = await client.call_tool("export_history", {"chat_ref": "1", "save": True})
        assert not exported.is_error
        saved = Path(exported.structured_content["export"]["saved_file"]["local_path"])
    return saved


@pytest.mark.parametrize("schema_mode", ["compact", "full"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("kind", ["stdio", "http", "connect"])
async def test_monitoring_restart_and_archives_across_transports(tmp_path, schema_mode, mode, kind):
    async with asyncio.timeout(60):
        if kind == "stdio":
            saved = await exercise(kind, tmp_path, mode, schema_mode)
        else:
            async with running_server(tmp_path, schema_mode=schema_mode, archive=True) as (
                settings,
                _,
            ):
                saved = await exercise(kind, tmp_path, mode, schema_mode, settings)
    assert await asyncio.to_thread(saved.exists)
