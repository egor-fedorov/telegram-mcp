"""Expose the shared loopback server to clients that launch STDIO subprocesses."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx2
from mcp.client import Client
from mcp.client.session import ElicitationFnT
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    CallToolRequestParams,
    CallToolResult,
    CompleteRequestParams,
    ElicitRequestFormParams,
    ErrorData,
    GetPromptRequestParams,
    InputRequiredResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    TextContent,
)

from telegram_mcp.adapters.bridge_status import status_result, status_tool
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import endpoint
from telegram_mcp.adapters.version import get_build_info
from telegram_mcp.domain.models import ToolError


def unavailable() -> ToolError:
    return ToolError(
        "SHARED_SERVER_UNAVAILABLE",
        "Cannot connect to the shared Telegram server. Start 'serve --transport http' "
        "and use the same http_port and http_token_path for connect. If it is already running, "
        "check that both commands use the same token file.",
    )


def confirmation_callback(downstream) -> ElicitationFnT | None:
    capabilities = downstream.session.client_capabilities
    form = capabilities.elicitation if capabilities is not None else None
    if form is None or (form.form is None and form.model_fields_set):
        return None

    async def elicit(context, params):
        if not isinstance(params, ElicitRequestFormParams):
            return ErrorData(code=INVALID_REQUEST, message="Only form confirmation is supported")
        return await downstream.session.elicit_form(
            params.message,
            params.requested_schema,
            related_request_id=downstream.request_id,
        )

    return elicit


async def confirmed_call(
    client: Client, context, params: CallToolRequestParams
) -> CallToolResult | InputRequiredResult:
    if context.protocol_version >= "2026-07-28":
        await client.list_tools()
        return await client.session.call_tool(
            params.name,
            params.arguments,
            meta=params.meta,
            input_responses=params.input_responses,
            request_state=params.request_state,
            allow_input_required=True,
        )
    return await client.call_tool(params.name, params.arguments, meta=params.meta)


async def routed_call(
    upstream: Client,
    context,
    params: CallToolRequestParams,
    confirmation_client: Callable[[ElicitationFnT | None], AbstractAsyncContextManager[Client]]
    | None,
) -> CallToolResult | InputRequiredResult:
    if confirmation_client is not None and params.name in {
        "update_draft",
        "send_message",
        "send_file",
        "invoke_button",
        "get_status",
        "resolve_recipient",
    }:
        # Bind confirmation and capability reporting to the originating downstream client.
        async with confirmation_client(confirmation_callback(context)) as isolated:
            return await confirmed_call(isolated, context, params)
    return await upstream.call_tool(params.name, params.arguments, meta=params.meta)


def create_bridge(
    upstream: Client,
    *,
    confirmation_client: Callable[[ElicitationFnT | None], AbstractAsyncContextManager[Client]]
    | None = None,
) -> Server:
    connect_info = get_build_info()
    upstream_version = getattr(upstream.server_info, "version", None)

    async def forward[R](call: Callable[[], Awaitable[R]]) -> R:
        try:
            return await call()
        except MCPError:
            raise
        except Exception:
            issue = unavailable().issue()
            raise MCPError(
                INTERNAL_ERROR,
                issue.model_dump_json(exclude_none=True),
                data=issue.model_dump(mode="json", exclude_none=True),
            ) from None

    async def list_tools(_context, params: PaginatedRequestParams | None):
        result = await forward(
            lambda: upstream.list_tools(cursor=params.cursor if params is not None else None)
        )
        return result.model_copy(update={"tools": [status_tool(tool) for tool in result.tools]})

    async def list_resources(_context, params: PaginatedRequestParams | None):
        return await forward(
            lambda: upstream.list_resources(cursor=params.cursor if params is not None else None)
        )

    async def list_resource_templates(_context, params: PaginatedRequestParams | None):
        return await forward(
            lambda: upstream.list_resource_templates(
                cursor=params.cursor if params is not None else None
            )
        )

    async def read_resource(_context, params: ReadResourceRequestParams):
        return await forward(lambda: upstream.read_resource(params.uri))

    async def call_tool(
        _context, params: CallToolRequestParams
    ) -> CallToolResult | InputRequiredResult:
        try:
            result = await routed_call(upstream, _context, params, confirmation_client)
            if params.name == "get_status" and isinstance(result, CallToolResult):
                return status_result(result, connect_info, upstream_version)
            return result
        except MCPError:
            raise
        except Exception:
            return CallToolResult(
                content=[TextContent(type="text", text=unavailable().issue().model_dump_json())],
                is_error=True,
            )

    async def list_prompts(_context, params: PaginatedRequestParams | None):
        return await forward(
            lambda: upstream.list_prompts(cursor=params.cursor if params is not None else None)
        )

    async def get_prompt(_context, params: GetPromptRequestParams):
        return await forward(lambda: upstream.get_prompt(params.name, params.arguments))

    async def complete(_context, params: CompleteRequestParams):
        return await forward(
            lambda: upstream.complete(
                params.ref,
                {"name": params.argument.name, "value": params.argument.value},
                params.context.arguments if params.context is not None else None,
            )
        )

    # Advertise only what the shared server itself supports.
    capabilities = upstream.server_capabilities
    prompts = capabilities.prompts is not None
    completions = capabilities.completions is not None
    info = upstream.server_info
    return Server(
        info.name if info is not None else "Telegram Context",
        version=info.version if info is not None else "",
        instructions=upstream.instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_resource_templates,
        on_read_resource=read_resource,
        on_list_prompts=list_prompts if prompts else None,
        on_get_prompt=get_prompt if prompts else None,
        on_completion=complete if completions else None,
    )


@asynccontextmanager
async def shared_client(
    settings: Settings, elicitation_callback: ElicitationFnT | None = None
) -> AsyncGenerator[Client]:
    token = load_token(settings.http_token_path)
    try:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx2.Timeout(30, read=300),
            trust_env=False,
        ) as http:
            async with Client(
                streamable_http_client(endpoint(settings), http_client=http),
                cache=None,
                elicitation_callback=elicitation_callback,
                read_timeout_seconds=300,
            ) as upstream:
                yield upstream
    except ToolError:
        raise
    except Exception:
        raise unavailable() from None


async def connect(settings: Settings) -> None:
    async with shared_client(settings) as upstream:
        proxy = create_bridge(
            upstream, confirmation_client=lambda callback: shared_client(settings, callback)
        )
        async with stdio_server() as (read_stream, write_stream):
            await proxy.run(read_stream, write_stream, proxy.create_initialization_options())
