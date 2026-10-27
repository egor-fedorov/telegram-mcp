import asyncio
import base64
import sys
from pathlib import Path

import httpx2
import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    BlobResourceContents,
    ResourceLink,
    TextContent,
)

from telegram_mcp.adapters.config import save_settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.transport import endpoint
from tests.support.downloads import setup_downloads
from tests.support.resources import (
    MAX_BYTES,
    URI,
    assert_binary_read,
    assert_compact_download,
)
from tests.support.transport import running_resource_server

# Real transports and subprocesses; excluded from make test-fast.
pytestmark = pytest.mark.slow


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
async def test_stdio_delivers_full_size_resource(tmp_path: Path, mode: str) -> None:
    script = (
        "from pathlib import Path; "
        "from tests.support.resources import ResourceService; "
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
                        assert len((await client.list_resource_templates()).resource_templates) == 2
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
                    assert len((await client.list_resource_templates()).resource_templates) == 2
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
