"""Selected download contracts and binary resource delivery over real MCP transports."""

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    ServerCapabilities,
)

from telegram_mcp.adapters.bridge import create_bridge, shared_client
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.domain.models import ToolError
from tests.support.resources import (
    URI,
    ResourceService,
    assert_binary_read,
    assert_compact_download,
)
from tests.support.transport import running_resource_server


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_download_tool_and_resource_contract(tmp_path: Path, mode: str) -> None:
    service = ResourceService(tmp_path)
    async with Client(
        create_server(service.service(), schema_mode="full"), mode=mode, cache=None
    ) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        tool = tools["download_attachment"]
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is False
        assert tool.annotations.open_world_hint is True
        assert tool.input_schema["required"] == ["attachment_id"]
        assert tool.output_schema is not None
        assert tool.output_schema["properties"]["message_ref"]["type"] == "string"
        assert (await client.list_resources()).resources == []
        templates = (await client.list_resource_templates()).resource_templates
        assert len(templates) == 2
        assert templates[0].uri_template == "telegram-attachment://downloads/{token}"
        assert templates[0].mime_type is None
        invalid = await client.call_tool("download_attachment", {"attachment_id": " "})
        assert invalid.is_error and not service.download_calls
        result = await client.call_tool("download_attachment", {"attachment_id": "1:1:document"})
        uri = assert_compact_download(result, 1024)
        await assert_binary_read(client, uri, 1024)
        await assert_binary_read(client, uri, 1024)
        assert service.download_calls == ["1:1:document"]
        assert service.read_calls == [URI, URI]
        assert (await client.list_resources()).resources == []
        assert (await client.list_resource_templates()).resource_templates == templates
    assert service.closed


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    "failure,code,issue_code",
    [
        (
            ToolError("DOWNLOAD_UNAVAILABLE", "Expired download."),
            INVALID_PARAMS,
            "DOWNLOAD_UNAVAILABLE",
        ),
        (ToolError("ACCESS_DENIED", "Source is not permitted."), INTERNAL_ERROR, "ACCESS_DENIED"),
        (ToolError("RATE_LIMITED", "Wait before retrying.", 10), INTERNAL_ERROR, "RATE_LIMITED"),
        (OSError("synthetic-private-path"), INTERNAL_ERROR, None),
    ],
)
async def test_resource_errors_are_safe_protocol_errors(
    tmp_path: Path, mode: str, failure: Exception, code: int, issue_code: str | None
) -> None:
    service = ResourceService(tmp_path)
    service.failure = failure
    async with Client(
        create_server(service.service(), schema_mode="full"), mode=mode, cache=None
    ) as client:
        with pytest.raises(MCPError) as caught:
            await client.read_resource(URI)
        assert caught.value.code == code
        assert "synthetic-private-path" not in caught.value.message
        if issue_code is not None:
            assert json.loads(caught.value.message)["code"] == issue_code
            assert caught.value.data["code"] == issue_code
            if issue_code == "RATE_LIMITED":
                assert caught.value.data["retry_after_seconds"] == 10


async def test_resource_uses_the_same_gate_as_tools(tmp_path: Path) -> None:
    service = ResourceService(tmp_path)
    gate = ToolCallGate(timeout_seconds=0.01, capacity=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_gate() -> None:
        entered.set()
        await release.wait()

    async with Client(create_server(service.service(), gate=gate, schema_mode="full")) as client:
        active = asyncio.create_task(gate.run(hold_gate))
        try:
            await entered.wait()
            with pytest.raises(MCPError) as caught:
                await client.read_resource(URI)
            assert caught.value.data["code"] == "SERVER_BUSY"
            assert not service.read_calls
        finally:
            release.set()
            await active
        await assert_binary_read(client, URI, 1024)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_bridge_preserves_resource_failures(tmp_path: Path, mode: str) -> None:
    async with running_resource_server(tmp_path, 1024) as (settings, service):
        service.failure = ToolError("ACCESS_DENIED", "Source is not permitted.")
        async with shared_client(settings) as upstream:
            async with Client(create_bridge(upstream), mode=mode, cache=None) as client:
                with pytest.raises(MCPError) as direct:
                    await upstream.read_resource(URI)
                with pytest.raises(MCPError) as proxied:
                    await client.read_resource(URI)
                assert proxied.value.error == direct.value.error


@pytest.mark.parametrize("operation", ["read", "list", "templates"])
async def test_bridge_reports_safe_resource_connection_errors(operation: str) -> None:
    class UnavailableClient:
        server_info = None
        instructions = None
        server_capabilities = ServerCapabilities()

        async def read_resource(self, _uri):
            raise OSError("private upstream details")

        async def list_resources(self, *, cursor=None):
            raise OSError("private upstream details")

        async def list_resource_templates(self, *, cursor=None):
            raise OSError("private upstream details")

    async with Client(create_bridge(cast(Client, UnavailableClient())), cache=None) as client:
        with pytest.raises(MCPError) as caught:
            if operation == "read":
                await client.read_resource(URI)
            elif operation == "list":
                await client.list_resources()
            else:
                await client.list_resource_templates()
        assert caught.value.code == INTERNAL_ERROR
        assert caught.value.data["code"] == "SHARED_SERVER_UNAVAILABLE"
        assert "private upstream details" not in caught.value.message
