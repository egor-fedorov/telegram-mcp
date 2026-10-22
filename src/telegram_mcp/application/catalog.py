"""Catalog browsing with fresh policy checks and immutable cursor snapshots."""

from dataclasses import dataclass

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.common import check_limit, fingerprint
from telegram_mcp.application.ports import ChatPort, Clock, CursorPort
from telegram_mcp.domain.folders import select_chats
from telegram_mcp.domain.models import Chat, ChatPage, Folder, Scope, ToolError


@dataclass
class ChatState:
    chats: list[Chat]
    index: int
    scope: Scope


class CatalogService:
    def __init__(self, chats: ChatPort, access: AccessFactory, cursors: CursorPort, clock: Clock):
        self.chats = chats
        self.access = access
        self.cursors = cursors
        self.clock = clock

    async def list_folders(self, query: str | None = None) -> list[Folder]:
        _, folders = await self.chats.catalog()
        return [f for f in folders if not query or query.casefold() in f.title.casefold()]

    async def list_chats(
        self,
        query: str | None = None,
        chat_type: str | None = None,
        folder_ids: list[int] | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> ChatPage:
        check_limit(limit)
        access = await self.access.load()
        chats, folders, allowed = access.chats, access.folders, access.allowed
        fp = fingerprint([query, chat_type, folder_ids])
        if cursor:
            state = self.cursors.get(cursor, "chats", fp)
            if not isinstance(state, ChatState):
                raise ToolError("INVALID_CURSOR", "Wrong cursor type.")
        else:
            selected = select_chats(
                chats, folders, allowed, folder_ids, query=query, chat_type=chat_type
            )
            state = ChatState(
                selected,
                0,
                Scope(
                    folder_ids=folder_ids or [],
                    chat_ids=[c.id for c in selected],
                    resolved_at=self.clock.utc_now(),
                ),
            )
        for chat in state.chats:
            access.require_access(chat.id)
        page = state.chats[state.index : state.index + limit]
        state.index += len(page)
        more = state.index < len(state.chats)
        token = self.cursors.put("chats", fp, state) if more else None
        return ChatPage(
            chats=page,
            next_cursor=token,
            scope=state.scope.model_copy(update={"complete": not more}),
        )

    async def get_chat(self, chat_ref: str) -> dict[str, object]:
        access = await self.access.load(explicit=True)
        chat = await self.chats.resolve_chat(chat_ref, refresh=True)
        await access.allow_explicit(chat.id)
        chat = access.public.get(chat.id, chat)
        return {"chat": chat.model_dump(mode="json"), **await self.chats.chat_details(chat)}
