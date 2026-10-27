"""Expose deliberate domain failures as MCP tool errors."""

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Protocol

from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.domain.models import ToolError


class Expose(Protocol):
    """Wraps a tool so it runs through the server's call gate and error mapping."""

    def __call__[**P, R](self, fn: Callable[P, Awaitable[R]], /) -> Callable[P, Awaitable[R]]: ...


def expose_tool_errors[**P, R](
    fn: Callable[P, Awaitable[R]], *, gate: ToolCallGate | None = None
) -> Callable[P, Awaitable[R]]:
    """Expose deliberate domain failures while leaving unexpected errors to SDK redaction."""

    @wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            if gate is not None:
                return await gate.run(lambda: fn(*args, **kwargs))
            return await fn(*args, **kwargs)
        except ToolError as exc:
            raise MCPToolError(exc.issue().model_dump_json(exclude_none=True)) from None

    return wrapped
