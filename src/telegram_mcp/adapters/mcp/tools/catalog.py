"""Folders, chats and forum topics."""

from typing import Annotated

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import StringConstraints

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import READONLY, FolderIDs, Limit
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import ChatType


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    @server.tool(annotations=READONLY)
    @expose
    async def list_folders(
        query: str | None = None,
    ) -> Annotated[CallToolResult, wire.FolderResult]:
        """Discover folder IDs, names, order and rules. Optional case-insensitive name substring."""
        return wire.encode(await service.list_folders(query))

    @server.tool(annotations=READONLY)
    @expose
    async def list_chats(
        query: str | None = None,
        chat_type: ChatType | None = None,
        folder_ids: FolderIDs | None = None,
        limit: Limit = 30,
        cursor: str | None = None,
    ) -> Annotated[CallToolResult, wire.ChatPage]:
        """List permitted chats, optionally from a union of folders. Page through all results.

        query matches chat title or username.
        Archive chats are included unless folder rules exclude them.
        A cursor freezes the selected chat list; continue with the same filters.
        """
        return wire.encode(await service.list_chats(query, chat_type, folder_ids, limit, cursor))

    @server.tool(annotations=READONLY)
    @expose
    async def get_chat(chat_ref: str) -> Annotated[CallToolResult, wire.ChatDetails]:
        """Read permitted chat metadata by numeric ID, exact @username or t.me[/s]/username URL.

        Explicit public channels outside the allowlist require allow_public_channel_reads.
        Unknown numeric IDs may require resolving a username or URL first.
        """
        return wire.encode(await service.get_chat(chat_ref))

    @server.tool(annotations=READONLY)
    @expose
    async def list_topics(
        chat_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)],
        query: Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
        ]
        | None = None,
        limit: Limit = 30,
        cursor: str | None = None,
    ) -> Annotated[CallToolResult, wire.TopicPage]:
        """List topics in a permitted forum supergroup by ID, @username or t.me[/s] link.

        query optionally searches topic names. Public discovery/read permissions do not
        authorize forum groups. Follow next_cursor with the original selector and query;
        a short or empty page can still have a cursor. Results preserve Telegram order
        and report incomplete metadata in issues. Temporary failures retain a cursor;
        respect retry_after_seconds. Topic order and titles may change during traversal.
        Pass a topic ID to get_history or get_pinned_messages to read that topic.
        Read top_message_ref with get_messages, or ordinary root_message_ref with
        get_thread. General (ID 1) has no root_message_ref. Bot forums and monoforums
        are unsupported. Listing topics does not mark them read or modify them.
        """
        return wire.encode(await service.list_topics(chat_ref, query, limit, cursor))
