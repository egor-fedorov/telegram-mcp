"""Selected download contracts and binary resource delivery over real MCP transports."""

import asyncio
import base64
import hashlib
import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    BlobResourceContents,
    CallToolResult,
    ResourceLink,
    TextContent,
)

from telegram_mcp.adapters.bridge import create_bridge, shared_client
from telegram_mcp.adapters.config import Settings, save_settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.adapters.transport import create_http_app, endpoint
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import DownloadResult, MessageRef, ToolError
from tests.test_attachments import setup_downloads

MAX_BYTES = 20 * 1024 * 1024
URI = "telegram-attachment://downloads/synthetic-token"


def synthetic_bytes(size: int) -> bytes:
    """Exercise every byte value rather than compressible text or a tiny fixture."""
    pattern = bytes(range(256))
    return (pattern * ((size + len(pattern) - 1) // len(pattern)))[:size]


class ResourceService:
    """Isolate the public transport seam from Telegram and application storage tests."""

    def __init__(self, directory: Path, size: int = 1024) -> None:
        self.payload = synthetic_bytes(size)
        self.directory = directory
        self.download_calls: list[str] = []
        self.read_calls: list[str] = []
        self.failure: Exception | None = None
        self.closed = False

    def service(self) -> Service:
        return cast(Service, self)

    async def download_attachment(self, attachment_id: str) -> DownloadResult:
        self.download_calls.append(attachment_id)
        if self.failure is not None:
            raise self.failure
        return DownloadResult(
            attachment_id=attachment_id,
            message_ref=MessageRef(chat_id="1", message_id=1),
            name="binary.dat",
            mime_type="application/x-telegram-test",
            size=len(self.payload),
            sha256=hashlib.sha256(self.payload).hexdigest(),
            local_path=str(self.directory / "binary.dat"),
            resource_uri=URI,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    async def read_download_resource(self, uri: str) -> tuple[bytes, str]:
        self.read_calls.append(uri)
        if self.failure is not None:
            raise self.failure
        if uri != URI:
            raise ToolError("DOWNLOAD_UNAVAILABLE", "The download is unknown or expired.")
        return self.payload, "application/x-telegram-test"

    async def close(self) -> None:
        self.closed = True


def assert_compact_download(result: CallToolResult, size: int) -> str:
    assert not result.is_error
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["size"] == size
    assert payload["message_ref"] == "1:1"
    assert payload["sha256"] == hashlib.sha256(synthetic_bytes(size)).hexdigest()
    assert len(result.content) == 2
    text, link = result.content
    assert isinstance(text, TextContent)
    assert text.text == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert isinstance(link, ResourceLink)
    assert link.uri == payload["resource_uri"] == URI
    assert link.mime_type == payload["mime_type"] == "application/x-telegram-test"
    assert link.size == size
    assert len(result.model_dump_json()) < 4096
    assert "blob" not in payload
    return str(link.uri)


async def assert_binary_read(client: Client, uri: str, size: int) -> None:
    result = await client.read_resource(uri)
    assert len(result.contents) == 1
    resource = result.contents[0]
    assert isinstance(resource, BlobResourceContents)
    assert resource.uri == uri
    assert resource.mime_type == "application/x-telegram-test"
    assert base64.b64decode(resource.blob, validate=True) == synthetic_bytes(size)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_download_tool_and_resource_contract(tmp_path: Path, mode: str) -> None:
    service = ResourceService(tmp_path)
    async with Client(create_server(service.service()), mode=mode, cache=None) as client:
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
        assert len(templates) == 1
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
async def test_mcp_download_uses_real_access_and_storage(tmp_path: Path, mode: str) -> None:
    async with setup_downloads(tmp_path) as (service, backend, settings, _, clock):
        async with Client(
            create_server(service, owns_service=False), mode=mode, cache=None
        ) as client:
            settings.allow_attachment_downloads = False
            disabled = await client.call_tool(
                "download_attachment", {"attachment_id": "1:10:document"}
            )
            assert disabled.is_error
            assert isinstance(disabled.content[0], TextContent)
            assert "DOWNLOADS_DISABLED" in disabled.content[0].text
            assert not backend.backend_calls
            settings.allow_attachment_downloads = True
            result = await client.call_tool(
                "download_attachment", {"attachment_id": "1:10:document"}
            )
            assert not result.is_error and result.structured_content is not None
            payload = result.structured_content
            assert len(result.content) == 2 and isinstance(result.content[1], ResourceLink)
            uri = str(result.content[1].uri)
            assert uri == payload["resource_uri"]
            path = Path(payload["local_path"])
            assert await asyncio.to_thread(path.read_bytes) == backend.data
            resource = (await client.read_resource(uri)).contents[0]
            assert isinstance(resource, BlobResourceContents)
            assert resource.mime_type == "application/octet-stream"
            assert base64.b64decode(resource.blob) == backend.data
            settings.allowed_chat_ids.clear()
            with pytest.raises(MCPError) as denied:
                await client.read_resource(uri)
            assert denied.value.data["code"] == "ACCESS_DENIED"
            settings.allowed_chat_ids.append("1")
            clock.time += 900
            with pytest.raises(MCPError) as expired:
                await client.read_resource(uri)
            assert expired.value.data["code"] == "DOWNLOAD_UNAVAILABLE"
            assert backend.downloads == 1
    assert not await asyncio.to_thread(path.exists)


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
    async with Client(create_server(service.service()), mode=mode, cache=None) as client:
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
    gate = ToolCallGate(timeout_seconds=0.01)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_gate() -> None:
        entered.set()
        await release.wait()

    async with Client(create_server(service.service(), gate=gate)) as client:
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


@asynccontextmanager
async def running_resource_server(
    directory: Path, size: int
) -> AsyncIterator[tuple[Settings, ResourceService]]:
    service = ResourceService(directory, size)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        settings = Settings(
            http_port=listener.getsockname()[1], http_token_path=directory / "resource.token"
        )
        app = create_http_app(
            service.service(),
            load_token(settings.http_token_path, create=True),
            settings.http_port,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield settings, service
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_delivers_full_size_resource(tmp_path: Path, mode: str) -> None:
    script = (
        "from pathlib import Path; "
        "from tests.test_attachments_mcp import ResourceService; "
        "from telegram_mcp.adapters.mcp import create_server; "
        "import sys; "
        "create_server(ResourceService(Path(sys.argv[1]), int(sys.argv[2])).service()).run()"
    )
    command = StdioServerParameters(
        command=sys.executable, args=["-c", script, str(tmp_path), str(MAX_BYTES)]
    )
    async with asyncio.timeout(60):
        async with Client(command, mode=mode, cache=None) as client:
            result = await client.call_tool(
                "download_attachment", {"attachment_id": "1:1:document"}
            )
            await assert_binary_read(client, assert_compact_download(result, MAX_BYTES), MAX_BYTES)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("transport", ["http", "connect"])
async def test_http_and_connect_deliver_full_size_resource(
    tmp_path: Path, mode: str, transport: str
) -> None:
    async with running_resource_server(tmp_path, MAX_BYTES) as (settings, service):
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
                        result = await client.call_tool(
                            "download_attachment", {"attachment_id": "1:1:document"}
                        )
                        await assert_binary_read(
                            client, assert_compact_download(result, MAX_BYTES), MAX_BYTES
                        )
                        assert (await client.list_resources()).resources == []
                        assert len((await client.list_resource_templates()).resource_templates) == 1
            else:
                config = tmp_path / "resource-config.toml"
                save_settings(settings, config)
                command = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "telegram_mcp.cli", "--config", str(config), "connect"],
                )
                async with Client(command, mode=mode, cache=None) as client:
                    result = await client.call_tool(
                        "download_attachment", {"attachment_id": "1:1:document"}
                    )
                    await assert_binary_read(
                        client, assert_compact_download(result, MAX_BYTES), MAX_BYTES
                    )
                    assert (await client.list_resources()).resources == []
                    assert len((await client.list_resource_templates()).resource_templates) == 1
        assert service.download_calls == ["1:1:document"]
        assert service.read_calls == [URI]
        assert not service.closed
    assert service.closed


async def test_http_resource_requests_require_authentication(tmp_path: Path) -> None:
    async with running_resource_server(tmp_path, 1024) as (settings, service):
        async with httpx2.AsyncClient(trust_env=False) as client:
            response = await client.post(
                endpoint(settings),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "resources/read",
                    "params": {"uri": URI},
                },
            )
            assert response.status_code == 401
            assert service.read_calls == []


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
