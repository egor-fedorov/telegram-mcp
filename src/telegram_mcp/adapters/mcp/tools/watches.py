"""Named subscriptions: creation, polling, acknowledgement and deletion."""

from typing import Annotated

from mcp.server import MCPServer
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import AwareDatetime, Field

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import PERSISTENT_READ, READONLY, FolderIDs, Limit
from telegram_mcp.application.service import Service


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    @server.tool(annotations=PERSISTENT_READ)
    @expose
    async def create_watch(
        name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")],
        chat_refs: Annotated[list[str], Field(min_length=1, max_length=20)] | None = None,
        folder_ids: FolderIDs | None = None,
        date_from: AwareDatetime | None = None,
    ) -> Annotated[CallToolResult, wire.WatchInfo]:
        """Create a named on-demand subscription with exactly one source selector.

        Start now, or supply date_from for available older messages. Folder membership
        refreshes between passes; new members start at the previous completed pass.
        Identical creation retries preserve positions. No scheduled/background polling.
        Shared clients consume the same named subscription; use different names for
        independent consumers. Local positions persist until explicit deletion.
        Use at most 20 chat_refs; equivalent links resolve once. Use folders for larger
        selections. Creation has a 35-second deadline; respect retry delays on failure.
        """
        return wire.encode(await service.create_watch(name, chat_refs, folder_ids, date_from))

    @server.tool(annotations=READONLY)
    @expose
    async def list_watches() -> Annotated[CallToolResult, wire.WatchList]:
        """Find saved subscription names, source counts and pending batches after a new session."""
        return wire.encode(await service.list_watches())

    @server.tool(annotations=PERSISTENT_READ)
    @expose
    async def get_watch_changes(
        name: str,
        limit: Limit = 30,
        ack_batch_id: str | None = None,
        save: bool = False,
    ) -> Annotated[CallToolResult, wire.WatchChangesResult]:
        """Poll a named subscription without managing per-chat tokens.

        A non-null batch_id must be acknowledged after processing, even when complete.
        Pass the processed batch as ack_batch_id to acknowledge and poll in one call,
        or use ack_watch_changes. Retrying that acknowledgement cannot confirm a newer batch.
        Until then the same saved batch is replayed, including after restart; limit
        applies only to new batches. Empty progress needs no acknowledgement.
        save=true (requires archive_directory) writes each batch to a permanent JSONL copy
        before it can be acknowledged and returns saved_file; retries reuse that copy.
        Continue incomplete passes by name; source order is fair, not globally sorted.
        Check issues/omissions and retry delays. Only the last 100 known message IDs
        per source are checked for revisions; this is not a historical event log.
        """
        return wire.encode(await service.get_watch_changes(name, limit, ack_batch_id, save))

    @server.tool(annotations=PERSISTENT_READ)
    @expose
    async def ack_watch_changes(
        name: str, batch_id: str
    ) -> Annotated[CallToolResult, wire.WatchAcknowledgement]:
        """Confirm a processed batch and atomically advance its subscription positions.

        Only acknowledge after processing its messages and omissions. A duplicate
        acknowledgement is safe; an old ID cannot acknowledge a newer pending batch.
        """
        return wire.encode(await service.ack_watch_changes(name, batch_id))

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
        )
    )
    @expose
    async def delete_watch(name: str) -> Annotated[CallToolResult, wire.WatchDeletion]:
        """Explicitly delete a subscription, positions and any unacknowledged batch.

        This discards local monitoring progress. Telegram and permanent archive copies
        are unchanged. Recreating a watch starts a new coverage interval.
        """
        return wire.encode(await service.delete_watch(name))
