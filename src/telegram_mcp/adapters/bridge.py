"""Expose the shared loopback server to clients that launch STDIO subprocesses."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolRequestParams, CallToolResult, PaginatedRequestParams, TextContent

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import endpoint
from telegram_mcp.domain.models import ToolError


def unavailable() -> ToolError:
    return ToolError(
        "SHARED_SERVER_UNAVAILABLE",
        "Cannot connect to the shared Telegram server. Start 'serve --transport http' "
        "and use the same http_port and http_token_path for connect. If it is already running, "
        "check that both commands use the same token file.",
    )


def create_bridge(upstream: Client) -> Server:
    async def list_tools(_context, params: PaginatedRequestParams | None):
        return await upstream.list_tools(cursor=params.cursor if params is not None else None)

    async def call_tool(_context, params: CallToolRequestParams) -> CallToolResult:
        try:
            return await upstream.call_tool(params.name, params.arguments, meta=params.meta)
        except MCPError:
            raise
        except Exception:
            return CallToolResult(
                content=[TextContent(type="text", text=unavailable().issue().model_dump_json())],
                is_error=True,
            )

    info = upstream.server_info
    return Server(
        info.name if info is not None else "Telegram Context",
        version=info.version if info is not None else "",
        instructions=upstream.instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


@asynccontextmanager
async def shared_client(settings: Settings) -> AsyncGenerator[Client]:
    token = load_token(settings.http_token_path)
    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx2.Timeout(30, read=300),
            trust_env=False,
        ) as http:
            async with Client(
                streamable_http_client(endpoint(settings), http_client=http), cache=None
            ) as upstream:
                yield upstream
    except ToolError:
        raise
    except Exception:
        raise unavailable() from None


async def connect(settings: Settings) -> None:
    async with shared_client(settings) as upstream:
        proxy = create_bridge(upstream)
        async with stdio_server() as (read_stream, write_stream):
            await proxy.run(read_stream, write_stream, proxy.create_initialization_options())
