"""Locally permitted drafts, immutable files, sends and bot callbacks."""

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

WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)
LOCAL = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def register(server: MCPServer[None], service: Service, expose: Expose, gate: ToolCallGate) -> None:
    rounds = ConfirmationRounds()

    @server.tool(annotations=LOCAL)
    @expose
    async def register_file(
        attachment_id: str | None = None, path: str | None = None
    ) -> Annotated[CallToolResult, dto.RegisteredFile]:
        """Register immutable file bytes for permitted sends. Supply attachment_id XOR path.

        Local paths must be inside allowed_upload_directories. Telegram sources require
        download permission and are rechecked on reuse. Limits: 20 MiB/file, 100 MiB/32 files.
        Files persist until removed with the local files CLI; no automatic eviction.
        """
        return wire.encode(await service.register_file(attachment_id, path))

    @server.tool(annotations=READONLY)
    @expose
    async def list_files() -> Annotated[CallToolResult, dto.FileList]:
        """List this account's reusable files whose source is still accessible."""
        return wire.encode(await service.list_files())

    @server.tool(annotations=READONLY)
    @expose
    async def get_draft(chat_ref: str) -> Annotated[CallToolResult, dto.Draft]:
        """Read the native Telegram draft and revision for one allowed ordinary dialog."""
        return wire.encode(await service.get_draft(chat_ref))

    @server.tool(annotations=READONLY)
    @expose
    async def list_drafts(
        limit: int = 30, cursor: str | None = None
    ) -> Annotated[CallToolResult, dto.DraftList]:
        """List permitted drafts with read checks; all-chat writes scan only readable sources."""
        return wire.encode(await service.list_drafts(limit, cursor))

    @server.tool(annotations=WRITE)
    @expose
    async def create_draft(
        chat_ref: str, text: str, file_id: str | None = None
    ) -> Annotated[CallToolResult, dto.Draft]:
        """Create a native text-only draft when empty. file_id is currently unsupported.

        Changes Telegram state but does not send. Requires allow_drafts and read+write grants.
        An existing draft returns DRAFT_EXISTS; read it before choosing update_draft.
        Supplying file_id returns DRAFT_ATTACHMENT_UNSUPPORTED before any upload or write.
        """
        return wire.encode(await service.create_draft(chat_ref, text, file_id))

    @server.tool(annotations=WRITE)
    @rounds.wrap
    @expose
    async def update_draft(
        chat_ref: str,
        text: str,
        expected_revision: str,
        ctx: Context[None, Any],
        file_id: str | None = None,
    ) -> Annotated[CallToolResult, dto.Draft]:
        """Replace a draft at its expected revision; user overwrites follow local form policy.

        Server-owned unchanged drafts need no prompt. Telegram has no atomic compare-and-swap;
        a user edit during the save can still race. Only plain text is supported.
        Supplying file_id returns DRAFT_ATTACHMENT_UNSUPPORTED before any upload or write.
        """
        return wire.encode(
            await service.update_draft(
                chat_ref, text, expected_revision, file_id, rounds.confirmation(ctx, gate)
            )
        )

    @server.tool(annotations=WRITE)
    @rounds.wrap
    @expose
    async def send_message(
        chat_ref: str,
        text: str,
        idempotency_key: str,
        ctx: Context[None, Any],
        reply_to: str | None = None,
    ) -> Annotated[CallToolResult, dto.SendReceipt]:
        """Send one plain-text message under local permission/form policies. No automatic previews.

        Use an ID, username or t.me chat link. One-off grants always require a client form.
        Other grants use require_send_message_confirmation. Keep the idempotency_key and exact
        arguments after a failure/unknown outcome; a new key may send twice. Maximum 4096 UTF-16
        units. No splitting, paid sends, scheduling or automatic draft clearing.
        """
        return wire.encode(
            await service.send_message(
                chat_ref, text, idempotency_key, reply_to, rounds.confirmation(ctx, gate)
            )
        )

    @server.tool(annotations=WRITE)
    @rounds.wrap
    @expose
    async def send_file(
        chat_ref: str,
        file_id: str,
        caption: str,
        idempotency_key: str,
        ctx: Context[None, Any],
        reply_to: str | None = None,
    ) -> Annotated[CallToolResult, dto.SendReceipt]:
        """Send one registered file plus plain caption under local permission/form policies.

        Requires allow_send_files and a recipient grant. Caption maximum 1024 UTF-16 units.
        Uses require_send_file_confirmation; one-off grants always require a client form.
        Reuse the same key and arguments to recover an uncertain send. No bulk sending.
        """
        return wire.encode(
            await service.send_file(
                chat_ref,
                file_id,
                caption,
                idempotency_key,
                reply_to,
                rounds.confirmation(ctx, gate),
            )
        )

    @server.tool(annotations=READONLY)
    @expose
    async def get_send_status(
        operation_id: str,
    ) -> Annotated[CallToolResult, dto.SendReceipt | dto.CallbackReceipt]:
        """Read a durable operation receipt in result, without dispatching or retrying it."""
        # A union's schema requires a result envelope even for explicit CallToolResult output.
        return wire.encode({"result": await service.get_send_status(operation_id)})

    @server.tool(annotations=WRITE)
    @rounds.wrap
    @expose
    async def invoke_button(
        message_ref: str,
        button_id: str,
        expected_revision: str,
        idempotency_key: str,
        ctx: Context[None, Any],
    ) -> Annotated[CallToolResult, dto.CallbackReceipt]:
        """Invoke one ordinary callback in a permitted private bot dialog under local form policy.

        Requires allow_bot_callbacks, read+write grants and the exact current message revision.
        Forms follow require_bot_callback_confirmation in the server's local configuration.
        No URL/auth/payment/WebApp/2FA/contact/location actions. Duplicate keys replay receipts;
        unknown callbacks are never retried. Observed same-peer message refs are bounded to
        five seconds/20 items, incomplete, and are not proof the callback caused them.
        """
        return wire.encode(
            await service.invoke_button(
                message_ref,
                button_id,
                expected_revision,
                idempotency_key,
                rounds.confirmation(ctx, gate),
            )
        )
