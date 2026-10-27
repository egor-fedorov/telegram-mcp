"""Message retrieval: pins, search, history, references, albums, context and threads."""

from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp_types import CallToolResult
from pydantic import AwareDatetime, Field, StringConstraints

from telegram_mcp.adapters import wire
from telegram_mcp.adapters.mcp.errors import Expose
from telegram_mcp.adapters.mcp.params import (
    READONLY,
    Budget,
    ChatID,
    ChatIDs,
    FolderIDs,
    Limit,
    TopicID,
)
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import ContentType, MessageRef


def register(server: MCPServer[None], service: Service, expose: Expose) -> None:
    @server.tool(annotations=READONLY)
    @expose
    async def get_pinned_messages(
        chat_ref: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)],
        limit: Limit = 30,
        cursor: str | None = None,
        topic_id: TopicID | None = None,
    ) -> Annotated[CallToolResult, wire.Page]:
        """Read a chat's pinned messages, newest first, by ID, @username or t.me[/s] link.

        Omit topic_id for chat-wide pins, or supply a positive forum topic ID (General is 1).
        Topics require ordinary access to a supported forum supergroup; public-channel
        permission does not authorize forum groups. Chat-wide pins retain the existing
        public-channel access policy. Text budgets and cursor recovery apply.
        Follow next_cursor with the same chat_ref and topic_id; respect retry_after_seconds
        on temporary failures. Pin membership can change during traversal. No messages are
        pinned, unpinned, or marked read by this tool.
        """
        return wire.encode(await service.get_pinned_messages(chat_ref, limit, cursor, topic_id))

    @server.tool(annotations=READONLY)
    @expose
    async def search_messages(
        query: Annotated[str, Field(max_length=1000)],
        chat_ids: ChatIDs | None = None,
        folder_ids: FolderIDs | None = None,
        date_from: AwareDatetime | None = None,
        date_to: AwareDatetime | None = None,
        sender_id: ChatID | None = None,
        content_type: ContentType | None = None,
        has_link: bool | None = None,
        limit: Limit = 30,
        cursor: str | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
        ordering: Literal["page", "global"] = "page",
    ) -> Annotated[CallToolResult, wire.Page]:
        """Search permitted chats with early results, sorted within each page by default.

        ordering=page visits sources fairly without waiting for every source. Ordering
        between pages is not guaranteed. Use ordering=global for a strictly ordered
        traversal, which can require empty preparation pages. The response reports the
        actual ordering guarantee. order chooses newest_first or oldest_first. Repeat
        both parameters with a cursor. Restricted searches only fetch permitted chats.

        Dates include a timezone; interval is [date_from, date_to). Folder IDs form a union;
        explicit chat_ids intersect that union. Omitted selectors use all permitted dialogs.
        has_link=true selects URL-bearing posts, independently of content_type; only then
        may query be empty. has_link=false excludes links. Omit it for either kind.
        Uses Telegram keyword search, not semantic search. Native sender search and local
        date/content filtering are combined. Partial/empty pages may carry next_cursor.
        Configured public-channel access applies only to explicit chat_ids, never implicit scope.
        Temporary failures retain next_cursor; respect retry_after_seconds before continuing.
        """
        return wire.encode(
            await service.search_messages(
                query=query,
                chat_ids=chat_ids,
                folder_ids=folder_ids,
                date_from=date_from,
                date_to=date_to,
                sender_id=sender_id,
                content_type=content_type,
                has_link=has_link,
                limit=limit,
                cursor=cursor,
                order=order,
                ordering=ordering,
            )
        )

    @server.tool(annotations=READONLY)
    @expose
    async def get_history(
        chat_id: ChatID | None = None,
        date_from: AwareDatetime | None = None,
        date_to: AwareDatetime | None = None,
        order: Literal["newest_first", "oldest_first"] = "newest_first",
        limit: Limit = 30,
        cursor: str | None = None,
        chat_ref: str | None = None,
        topic_id: TopicID | None = None,
        chat_ids: ChatIDs | None = None,
        folder_ids: FolderIDs | None = None,
        ordering: Literal["page", "global"] | None = None,
        max_sources: Budget | None = None,
        max_messages: Budget | None = None,
    ) -> Annotated[CallToolResult, wire.Page]:
        """Read history over [date_from, date_to), or a chat's recent messages if dates are omitted.

        Supply exactly one of chat_id, chat_ref (numeric ID, @username, or t.me[/s]/username),
        chat_ids or folder_ids. chat_ids/folder_ids read several permitted sources in one
        traversal and require both dates. They default to ordering=page (early results,
        sorted within each page), max_sources=20 and max_messages=100; a larger selection
        returns SOURCE_LIMIT_EXCEEDED before reading. budget reports emitted messages;
        exhausted=true keeps next_cursor, and only an explicitly larger max_messages
        continues it. ordering=global waits for every source to keep strict order.
        Omit topic_id for chat-wide history, or supply a positive forum topic ID (General is 1)
        with a single chat. Topics require ordinary access to a supported forum supergroup.
        Follow next_cursor for a complete period, repeating the original arguments.
        Public channels outside the allowlist require allow_public_channel_reads and an
        explicit chat selector. Temporary failures retain next_cursor; respect
        retry_after_seconds before continuing.
        """
        return wire.encode(
            await service.get_history(
                chat_id=chat_id,
                date_from=date_from,
                date_to=date_to,
                order=order,
                limit=limit,
                cursor=cursor,
                chat_ref=chat_ref,
                topic_id=topic_id,
                chat_ids=chat_ids,
                folder_ids=folder_ids,
                ordering=ordering,
                max_sources=max_sources,
                max_messages=max_messages,
            )
        )

    @server.tool(annotations=READONLY)
    @expose
    async def get_messages(
        refs: Annotated[list[str | MessageRef], Field(min_length=1, max_length=100)],
        text_offset: Annotated[int, Field(ge=0)] = 0,
        text_limit: Annotated[int, Field(ge=1, le=100000)] = 12000,
    ) -> Annotated[CallToolResult, wire.MessageResult]:
        """Fetch posts by Telegram URL, 'chat_id:message_id', or {chat_id, message_id}.

        Includes authors, reply references, attachments, original links, available counters,
        aggregate reactions, polls and available web-page previews. Unknown metadata is omitted;
        previews come from the message payload without fetching external URLs. has_link is
        independent of content_type. No extra requests are made for metadata or voter/reactor
        identities. Polls retain content_type="other".
        Missing messages produce individual issues with zero-based input_index. Input order and
        duplicates are preserved. For truncated text call again with next_text_offset.
        Public channels outside the allowlist require allow_public_channel_reads;
        linked discussions need ordinary access.
        """
        return wire.encode(await service.get_messages(refs, text_offset, text_limit))

    @server.tool(annotations=READONLY)
    @expose
    async def get_album(
        ref: str | MessageRef,
        scan_limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 30,
    ) -> Annotated[CallToolResult, wire.AlbumResult]:
        """Read messages from one album around a member's Telegram URL or message reference.

        ref accepts a URL, 'chat_id:message_id', or {chat_id, message_id}. Scans up to
        scan_limit neighboring messages on each side, including intervening nonmembers,
        then returns matching messages from the same chat in oldest_first order.
        completeness is always unknown: Telegram provides no album manifest. Exhaustion
        fields describe history boundaries, not album completeness. There is no cursor.
        Missing anchors return MESSAGE_UNAVAILABLE; ungrouped anchors return NOT_AN_ALBUM.
        Neighbor failures return partial messages and issues. On temporary issues, retry the
        whole call after any retry_after_seconds. Initial access/anchor failures are errors.
        Text budgets apply; use get_messages with next_text_offset for truncated text.
        Explicit public channels require the existing read permission. This tool does not
        traverse linked discussions, download attachments, or mark messages read.
        """
        return wire.encode(await service.get_album(ref, scan_limit))

    @server.tool(annotations=READONLY)
    @expose
    async def get_context(
        ref: str | MessageRef,
        before: Annotated[int, Field(ge=0, le=30)] = 10,
        after: Annotated[int, Field(ge=0, le=30)] = 10,
        reply_depth: Annotated[int, Field(ge=0, le=10)] = 3,
    ) -> Annotated[CallToolResult, wire.ContextResult]:
        """Read a message, chronological neighbors, and a bounded chain of parent replies.

        This is temporal context; use get_thread for all comments/replies in a discussion.
        Cross-chat reply traversal respects the server access policy.
        """
        return wire.encode(await service.get_context(ref, before, after, reply_depth))

    @server.tool(annotations=READONLY)
    @expose
    async def get_thread(
        ref: str | MessageRef, limit: Limit = 30, cursor: str | None = None
    ) -> Annotated[CallToolResult, wire.Page]:
        """Read replies to a group thread root or comments on a channel post, newest first.

        For channels the linked discussion group must also be permitted. thread_root identifies
        the root in that group; get_messages can fetch it. Unsupported threads return an error.
        """
        return wire.encode(await service.get_thread(ref, limit, cursor))
