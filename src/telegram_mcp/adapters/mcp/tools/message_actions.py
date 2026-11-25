"""Locally enabled single-message previews, edits and deletions."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import CallToolResult, ToolAnnotations

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.confirmation import ConfirmationRounds
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import READONLY
from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.adapters.wire import outbound as dto
from telegram_mcp.application.service import Service
from telegram_mcp.domain.message_actions import Visibility

WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)


def register(server: MCPServer[None], service: Service, expose: Expose, gate: ToolCallGate) -> None:
    rounds = ConfirmationRounds()

    @server.tool(annotations=READONLY)
    @expose
    async def preview_message_action(
        message_ref: str | None = None, source_operation_id: str | None = None
    ) -> Annotated[CallToolResult, dto.ActionPreview]:
        """Inspect one message by reference or successful send operation.

        Returns the revision, excerpt and permitted actions with their form requirements.
        """
        return wire.encode(await service.preview_message_action(message_ref, source_operation_id))

    @server.tool(annotations=WRITE)
    @rounds.wrap
    @expose
    async def edit_message(
        text: str,
        expected_revision: str,
        idempotency_key: str,
        ctx: Context[None, Any],
        message_ref: str | None = None,
        source_operation_id: str | None = None,
    ) -> Annotated[CallToolResult, dto.ActionReceipt]:
        """Replace your plain text or simple caption at its previewed revision.

        Supply one target. Reuse the same key to recover the receipt.
        """
        return wire.encode(
            await service.edit_message(
                text,
                expected_revision,
                idempotency_key,
                message_ref,
                source_operation_id,
                rounds.confirmation(ctx, gate),
            )
        )

    @server.tool(annotations=WRITE)
    @rounds.wrap
    @expose
    async def delete_message(
        visibility: Visibility,
        expected_revision: str,
        idempotency_key: str,
        ctx: Context[None, Any],
        message_ref: str | None = None,
        source_operation_id: str | None = None,
    ) -> Annotated[CallToolResult, dto.ActionReceipt]:
        """Delete one previewed message with explicit visibility.

        Channels and supergroups support for_everyone only. A completed receipt records
        Telegram's acknowledgement.
        """
        return wire.encode(
            await service.delete_message(
                visibility,
                expected_revision,
                idempotency_key,
                message_ref,
                source_operation_id,
                rounds.confirmation(ctx, gate),
            )
        )
