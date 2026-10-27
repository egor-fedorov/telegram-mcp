import asyncio
import hashlib
import json
import sys

import httpx2
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ResourceLink, TextResourceContents

from telegram_mcp.adapters.config import save_settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import endpoint
from tests.support.fakes import msg
from tests.support.incremental import EXPORT_TEXT
from tests.support.transport import running_server

# Real transports and subprocesses; excluded from make test-fast.
pytestmark = pytest.mark.slow


async def assert_export_round_trip(client: Client):
    response = await client.call_tool("export_history", {"chat_ref": "1"})
    assert not response.is_error and response.structured_content is not None
    payload = response.structured_content
    assert payload["complete"] and payload["exported_count"] == 4
    assert len(response.model_dump_json()) < 5000
    link = response.content[1]
    assert isinstance(link, ResourceLink)
    resource = (await client.read_resource(str(link.uri))).contents[0]
    assert isinstance(resource, TextResourceContents)
    data = resource.text.encode("utf-8")
    assert len(data) == payload["export"]["size"] > 1024 * 1024
    assert hashlib.sha256(data).hexdigest() == payload["export"]["sha256"]
    rows = [json.loads(line) for line in resource.text.splitlines()]
    assert [row["message"]["text"] for row in rows[1:-1]] == [EXPORT_TEXT] * 4
    assert (await client.list_resources()).resources == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_transfers_full_unicode_export(tmp_path, mode):
    script = (
        "from pathlib import Path; import sys; "
        "from tests.support.incremental import export_stdio_service; "
        "from telegram_mcp.adapters.mcp import create_server; "
        "create_server(export_stdio_service(Path(sys.argv[1]))).run()"
    )
    command = StdioServerParameters(command=sys.executable, args=["-c", script, str(tmp_path)])
    async with asyncio.timeout(60):
        async with Client(command, mode=mode, cache=None) as client:
            await assert_export_round_trip(client)
    assert list((tmp_path / "exports").glob("*.jsonl")) == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("transport", ["http", "connect"])
async def test_shared_transports_transfer_exports_and_independent_checkpoints(
    tmp_path, mode, transport
):
    async with running_server(tmp_path) as (settings, backend):
        backend.stored_messages = [msg("1", mid, EXPORT_TEXT) for mid in range(1, 5)]
        async with asyncio.timeout(60):
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
                        await assert_export_round_trip(client)
            else:
                config = tmp_path / "config.toml"
                save_settings(settings, config)
                command = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "telegram_mcp.cli", "--config", str(config), "connect"],
                )
                async with Client(command, mode=mode, cache=None) as client:
                    await assert_export_round_trip(client)
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {load_token(settings.http_token_path)}"},
                trust_env=False,
            ) as http:
                async with (
                    Client(
                        streamable_http_client(endpoint(settings), http_client=http),
                        mode=mode,
                        cache=None,
                    ) as first,
                    Client(
                        streamable_http_client(endpoint(settings), http_client=http),
                        mode=mode,
                        cache=None,
                    ) as second,
                ):
                    a = await first.call_tool("get_changes", {"chat_ref": "1"})
                    b = await second.call_tool("get_changes", {"chat_ref": "1"})
                    assert a.structured_content and b.structured_content
                    backend.stored_messages.append(msg("1", 5))
                    for client, token in (
                        (first, a.structured_content["next_checkpoint"]),
                        (second, b.structured_content["next_checkpoint"]),
                    ):
                        result = await client.call_tool("get_changes", {"checkpoint": token})
                        assert not result.is_error and result.structured_content
                        assert [
                            event["message_ref"] for event in result.structured_content["changes"]
                        ] == ["1:5"]
    assert list((tmp_path / "exports").glob("*.jsonl")) == []
