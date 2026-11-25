"""Catalog browsing with fresh policy checks and immutable cursor snapshots."""

from dataclasses import dataclass

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.common import check_limit, fingerprint
from telegram_mcp.application.ports import ChatPort, Clock, CursorPort, FolderName
from telegram_mcp.domain.folders import require_folders, select_chats
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

    async def find_folder(self, title: str) -> FolderName:
        """Resolve one folder by exact, case-insensitive title; never guess between matches."""
        wanted = title.strip().casefold()
        if not wanted or len(wanted) > 128:
            raise ToolError("INVALID_ARGUMENT", "folder_name must contain 1..128 characters.")
        folders = await self.chats.folder_names()
        policy = self.access.policies.current()
        matches = [
            f
            for f in folders
            if policy.allows_folder(f.id) and f.title.strip().casefold() == wanted
        ]
        if not matches:
            raise ToolError("FOLDER_NOT_FOUND", "No folder has this exact title; use folder_id.")
        if len(matches) > 1:
            ids = ", ".join(str(f.id) for f in matches[:10])
            raise ToolError(
                "FOLDER_AMBIGUOUS", f"Several folders have this title (IDs: {ids}); use folder_id."
            )
        return matches[0]

    async def list_folders(self, query: str | None = None) -> list[Folder]:
        access = await self.access.load()
        return [
            folder.model_copy(
                update={
                    "included_chat_ids": [
                        cid for cid in folder.included_chat_ids if cid in access.allowed
                    ],
                    "excluded_chat_ids": [
                        cid for cid in folder.excluded_chat_ids if cid in access.allowed
                    ],
                    "pinned_chat_ids": [
                        cid for cid in folder.pinned_chat_ids if cid in access.allowed
                    ],
                }
            )
            for folder in access.folders
            if not query or query.casefold() in folder.title.casefold()
        ]

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
            require_folders(set(folder_ids or []), folders)
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
        if cursor:
            visible = {folder.id for folder in folders}
            page = [
                chat.model_copy(
                    update={
                        "matched_folder_ids": [
                            fid for fid in chat.matched_folder_ids if fid in visible
                        ],
                        "pinned_in_folder_ids": [
                            fid for fid in chat.pinned_in_folder_ids if fid in visible
                        ],
                    }
                )
                for chat in page
            ]
        state.index += len(page)
        more = state.index < len(state.chats)
        token = self.cursors.put("chats", fp, state) if more else None
        return ChatPage(
            chats=page,
            next_cursor=token,
            scope=state.scope.model_copy(
                update={
                    "complete": not more,
                    "pending_chat_ids": [chat.id for chat in state.chats[state.index :]],
                }
            ),
        )

    async def get_chat(self, chat_ref: str) -> dict[str, object]:
        access = await self.access.load(explicit=True)
        chat = await self.chats.resolve_chat(chat_ref, refresh=True)
        await access.allow_explicit(chat.id)
        chat = access.public.get(chat.id, chat)
        return {"chat": chat.model_dump(mode="json"), **await self.chats.chat_details(chat)}
