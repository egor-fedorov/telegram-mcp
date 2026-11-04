"""Request-scoped MCP form confirmation; unsupported clients always fail closed."""

import asyncio
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from mcp.server.mcpserver.context import Context
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel, Field

from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.application.outbound_ports import Confirm, ConfirmationPending
from telegram_mcp.domain.models import ToolError


class Approval(BaseModel):
    approve: bool = Field(
        default=False, strict=True, description="Approve this exact Telegram action"
    )


def supports_confirmation(context: Context[None, Any]) -> bool:
    capabilities = context.client_capabilities
    return (
        capabilities is not None
        and capabilities.elicitation is not None
        and (
            capabilities.elicitation.form is not None
            or not capabilities.elicitation.model_fields_set
        )
    )


def confirmation(context: Context[None, Any], gate: ToolCallGate | None = None) -> Confirm | None:
    if not supports_confirmation(context):
        return None

    async def ask(description: str) -> bool:
        async def wait() -> bool:
            try:
                async with asyncio.timeout(120):
                    result = await context.elicit(description, Approval)
                return result.action == "accept" and result.data.approve is True
            except TimeoutError:
                raise ToolError(
                    "CONFIRMATION_EXPIRED", "Confirmation expired; nothing was dispatched."
                ) from None
            except (MCPError, RuntimeError):
                raise ToolError(
                    "CONFIRMATION_UNSUPPORTED",
                    "The client could not complete MCP form elicitation.",
                ) from None

        return await gate.while_waiting(wait) if gate is not None else await wait()

    return ask


class FormRequired(ConfirmationPending):
    def __init__(self, result: InputRequiredResult):
        self.result = result


class ConfirmationRounds:
    """Short-lived one-use challenges for MCP's modern multi-round protocol."""

    def __init__(self):
        self.pending: dict[str, tuple[float, str]] = {}

    def wrap[**P, R](
        self, fn: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[R | InputRequiredResult]]:
        @wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R | InputRequiredResult:
            try:
                return await fn(*args, **kwargs)
            except FormRequired as exc:
                return exc.result

        return wrapped

    def confirmation(self, context: Context[None, Any], gate: ToolCallGate) -> Confirm | None:
        if not supports_confirmation(context):
            return None
        if context.protocol_version is None or context.protocol_version < "2026-07-28":
            return confirmation(context, gate)

        async def ask(description: str) -> bool:
            now = time.monotonic()
            digest = hashlib.sha256(description.encode()).hexdigest()
            self.pending = {token: value for token, value in self.pending.items() if value[0] > now}
            if context.request_state is not None:
                challenge = self.pending.pop(context.request_state, None)
                if challenge is None:
                    raise ToolError(
                        "CONFIRMATION_EXPIRED", "This confirmation expired or was already consumed."
                    )
                if challenge[1] != digest:
                    raise ToolError(
                        "CONFIRMATION_INVALID", "The confirmed action differs from this request."
                    )
                response = (context.input_responses or {}).get("approve")
                return (
                    isinstance(response, ElicitResult)
                    and response.action == "accept"
                    and (response.content or {}).get("approve") is True
                )
            if context.input_responses:
                raise ToolError(
                    "CONFIRMATION_INVALID", "Unsolicited confirmation responses are not accepted."
                )
            if len(self.pending) >= 16:
                raise ToolError("SERVER_BUSY", "Too many confirmations are pending.", 5)
            token = secrets.token_urlsafe(32)
            self.pending[token] = (now + 120, digest)
            raise FormRequired(
                InputRequiredResult(
                    request_state=token,
                    input_requests={
                        "approve": ElicitRequest(
                            params=ElicitRequestFormParams(
                                message=description,
                                requested_schema=Approval.model_json_schema(),
                            )
                        ),
                    },
                )
            )

        return ask
