"""Checkpointed change polling and history export."""

from typing import Annotated

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import AwareDatetime

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import PERSISTENT_READ, Limit
from telegram_mcp.application.service import Service
from telegram_mcp.domain.changes import ChangeOutput, ExportFormat


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    @server.tool(annotations=PERSISTENT_READ)
    @expose
    async def get_changes(
        chat_ref: str | None = None,
        checkpoint: str | None = None,
        limit: Limit = 30,
        output: ChangeOutput = "inline",
        save: bool = False,
    ) -> Annotated[CallToolResult, wire.ChangesResult]:
        """Poll one selected chat for new messages and available recent revisions.

        Supply chat_ref only to establish a baseline at the current head, without old
        history. Then supply checkpoint only. Rechecks the last 100 known messages;
        intermediate edits and all historical deletions cannot be recovered. complete
        means this pass finished; its next_checkpoint starts the next poll. Unavailable
        does not prove deletion. Retry old tokens safely, allowing duplicate revisions.
        Latest chain checkpoints survive 30 days; superseded versions may be evicted sooner.
        Inline text uses normal continuation offsets. output=jsonl/markdown returns a
        temporary file/resource with full records instead of inline changes. No binary
        attachment downloads. save=true retains the file in archive_directory.
        Respect retry delays and issues.
        """
        return wire.encode(await service.get_changes(chat_ref, checkpoint, limit, output, save))

    @server.tool(annotations=PERSISTENT_READ)
    @expose
    async def export_history(
        chat_ref: str | None = None,
        date_from: AwareDatetime | None = None,
        date_to: AwareDatetime | None = None,
        checkpoint: str | None = None,
        format: ExportFormat = "jsonl",
        save: bool = False,
    ) -> Annotated[CallToolResult, wire.ExportHistoryResult]:
        """Export one selected chat's original history as JSONL or Markdown.

        Start with chat_ref and optional dates (lower inclusive, upper exclusive),
        then continue with checkpoint only and the desired format. Upper date and ID
        are frozen; each part has at most 1000 records/20 MiB within RPC/time budgets.
        Read the returned resource explicitly before its 15-minute expiry. Files are
        local to the server, temporary, and include source references and full text;
        attachments are metadata only. Protected/expiring records are omitted explicitly.
        complete means the range was scanned; omitted_count and issues still matter.
        A non-null next_checkpoint survives restart; an empty part can need continuation.
        On temporary errors continue the returned token after any retry delay. On tool
        errors the original token remains usable. save=true retains a permanent archive copy.
        No snapshot consistency across Telegram edits.
        """
        return wire.encode(
            await service.export_history(chat_ref, date_from, date_to, checkpoint, format, save)
        )
