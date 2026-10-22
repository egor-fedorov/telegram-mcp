"""Chat catalog, entity resolution, and related chat metadata."""

import asyncio
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from telethon import functions, types, utils

from telegram_mcp.adapters.telegram.mapping import (
    chat_from_entity,
    folder_from_tl,
    peer_id,
    public_channel_from_entity,
    utc,
)
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import Chat, Folder, ToolError


@dataclass
class _DialogView:
    entity: Any
    dialog: Any
    archived_override: bool = False

    @property
    def archived(self) -> bool:
        return self.archived_override or self.dialog.folder_id is not None

    @property
    def unread_count(self) -> int:
        return self.dialog.unread_count

    @property
    def unread_mentions_count(self) -> int:
        return self.dialog.unread_mentions_count


@dataclass
class _CatalogBuilder:
    last_used: float
    auth_generation: int
    dialogs: dict[str, _DialogView] = field(default_factory=dict)
    phase: str = "pinned"
    pinned_folder_id: int = 0
    offset_date: datetime | None = None
    offset_id: int = 0
    offset_peer: Any = field(default_factory=types.InputPeerEmpty)
    offsets: set[tuple[str, int, datetime]] = field(default_factory=set)
    filters: list[Any] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    self_id: str | None = None
    retry_at: float = 0
    retry_error: ToolError | None = None


@dataclass
class _CatalogSnapshot:
    chats: list[Chat]
    folders: list[Folder]
    completed_at: float
    generation: int


class ChatGateway:
    def __init__(
        self,
        runtime: TelegramRuntime,
        *,
        clock: Clock,
        max_dialogs: int,
        cache: dict[str, Chat] | None = None,
        catalog_cache_ttl_seconds: float = 30,
    ):
        self.runtime = runtime
        self.clock = clock
        self.max_dialogs = max_dialogs
        self.cache = cache if cache is not None else {}
        self.catalog_cache_ttl_seconds = catalog_cache_ttl_seconds
        self._snapshot: _CatalogSnapshot | None = None
        self._builder: _CatalogBuilder | None = None
        self._catalog_lock = asyncio.Lock()

    async def check_authorization(self) -> None:
        try:
            await self.runtime.verify_authorization()
        except ToolError as exc:
            self._snapshot = None
            if exc.code == "AUTH_REQUIRED":
                self._builder = None
                self.cache.clear()
            raise

    async def catalog(self, *, refresh: bool = False) -> tuple[list[Chat], list[Folder]]:
        async with self._catalog_lock:
            if refresh:
                self._snapshot = None
                self._builder = None
            try:
                client = await self.runtime.ensure()
                now = self.clock.monotonic()
                snapshot = self._snapshot
                if (
                    snapshot is not None
                    and snapshot.generation == self.runtime.generation
                    and now - snapshot.completed_at < self.catalog_cache_ttl_seconds
                ):
                    await self.check_authorization()
                    now = self.clock.monotonic()
                    if (
                        snapshot.generation == self.runtime.generation
                        and now - snapshot.completed_at < self.catalog_cache_ttl_seconds
                    ):
                        return self._copy_snapshot(snapshot)
                self._snapshot = None
                builder = self._builder
                if (
                    builder is None
                    or now - builder.last_used >= 900
                    or builder.auth_generation != self.runtime.auth_generation
                ):
                    builder = _CatalogBuilder(now, self.runtime.auth_generation)
                    self._builder = builder
                builder.last_used = now
                if builder.retry_error is not None and now < builder.retry_at:
                    raise ToolError(
                        builder.retry_error.code,
                        builder.retry_error.detail,
                        max(1, math.ceil(builder.retry_at - now)),
                    )
                for _ in range(5):
                    if self.clock.monotonic() - now >= 5:
                        break
                    await self._catalog_step(client, builder)
                    if builder.auth_generation != self.runtime.auth_generation:
                        raise ToolError(
                            "AUTH_REQUIRED", "Telegram session is no longer authorized. Run login."
                        )
                    if builder.phase == "complete":
                        snapshot = self._complete_catalog(builder)
                        self._snapshot = snapshot
                        self._builder = None
                        self.cache = {
                            chat.id: chat.model_copy(deep=True) for chat in snapshot.chats
                        }
                        return self._copy_snapshot(snapshot)
                raise ToolError(
                    "CATALOG_LOADING",
                    f"Catalog refresh is in progress ({len(builder.dialogs)} dialogs loaded). "
                    "Repeat this request to continue.",
                    1,
                )
            except ToolError as exc:
                self._snapshot = None
                if (
                    exc.code == "RATE_LIMITED"
                    and self._builder is not None
                    and self.clock.monotonic() >= self._builder.retry_at
                ):
                    self._builder.retry_at = self.clock.monotonic() + (exc.retry_after or 0)
                    self._builder.retry_error = exc
                if not exc.retryable:
                    self._builder = None
                if exc.code == "AUTH_REQUIRED":
                    self.cache.clear()
                raise
            except (AttributeError, TypeError, ValueError):
                self._builder = None
                self._snapshot = None
                raise ToolError(
                    "CATALOG_ERROR", "Telegram returned unusable dialog metadata."
                ) from None

    @staticmethod
    def _copy_snapshot(snapshot: _CatalogSnapshot) -> tuple[list[Chat], list[Folder]]:
        return (
            [chat.model_copy(deep=True) for chat in snapshot.chats],
            [folder.model_copy(deep=True) for folder in snapshot.folders],
        )

    async def _catalog_step(self, client: Any, builder: _CatalogBuilder) -> None:
        if builder.phase == "pinned":
            response = await self.runtime.call(
                client(functions.messages.GetPinnedDialogsRequest(builder.pinned_folder_id))
            )
            if not isinstance(response, types.messages.PeerDialogs):
                raise ToolError("CATALOG_ERROR", "Telegram returned unusable pinned dialogs.")
            self._validate_dialogs(response)
            additions, self_id = self._collect_dialogs(
                builder, response, archived=builder.pinned_folder_id == 1
            )
            builder.dialogs.update(additions)
            builder.self_id = self_id
            if builder.pinned_folder_id == 0:
                builder.pinned_folder_id = 1
            else:
                builder.phase = "dialogs"
        elif builder.phase == "dialogs":
            await self._dialog_page(client, builder)
        elif builder.phase == "filters":
            response = await self.runtime.call(client(functions.messages.GetDialogFiltersRequest()))
            filters = response.filters if hasattr(response, "filters") else response
            if not isinstance(filters, list):
                raise ToolError("CATALOG_ERROR", "Telegram returned unusable folder metadata.")
            builder.filters = filters
            builder.phase = self._auxiliary_phase(builder) or "complete"
        elif builder.phase == "self":
            builder.self_id = peer_id(await self.runtime.call(client.get_me(input_peer=True)))
            builder.phase = self._auxiliary_phase(builder) or "filters"
        elif builder.phase == "defaults":
            name, peer = next(
                (name, peer)
                for name, peer in (
                    ("channel", types.InputNotifyBroadcasts()),
                    ("group", types.InputNotifyChats()),
                    ("private", types.InputNotifyUsers()),
                )
                if name not in builder.defaults
            )
            builder.defaults[name] = await self.runtime.call(
                client(functions.account.GetNotifySettingsRequest(peer))
            )
            builder.phase = self._auxiliary_phase(builder) or "filters"

    @staticmethod
    def _auxiliary_phase(builder: _CatalogBuilder) -> str | None:
        if builder.self_id is None and any(
            isinstance(peer, types.InputPeerSelf)
            for raw in builder.filters
            for name in ("include_peers", "pinned_peers", "exclude_peers")
            for peer in getattr(raw, name, [])
        ):
            return "self"
        if (
            any(getattr(raw, "exclude_muted", False) for raw in builder.filters)
            and len(builder.defaults) < 3
        ):
            return "defaults"
        return None

    async def _dialog_page(self, client: Any, builder: _CatalogBuilder) -> None:
        limit = min(100, self.max_dialogs + 1 - len(builder.dialogs))
        response = await self.runtime.call(
            client(
                functions.messages.GetDialogsRequest(
                    offset_date=builder.offset_date,
                    offset_id=builder.offset_id,
                    offset_peer=builder.offset_peer,
                    limit=limit,
                    hash=0,
                    exclude_pinned=True,
                )
            )
        )
        if (
            not isinstance(response, (types.messages.Dialogs, types.messages.DialogsSlice))
            or len(response.dialogs) > limit
        ):
            raise ToolError("CATALOG_ERROR", "Telegram returned an unusable dialog page.")
        self._validate_dialogs(response)
        entities = {peer_id(entity): entity for entity in [*response.users, *response.chats]}
        messages = {
            (peer_id(message.peer_id), message.id): message
            for message in response.messages
            if getattr(message, "peer_id", None) is not None
        }
        additions, self_id = self._collect_dialogs(builder, response)
        finished = isinstance(response, types.messages.Dialogs) or len(response.dialogs) < limit
        next_offset = None
        next_peer = None
        if not finished:
            for raw in reversed(response.dialogs):
                if not isinstance(raw, types.Dialog):
                    continue
                cid = peer_id(raw.peer)
                message = messages.get((cid, raw.top_message))
                if message is not None and isinstance(getattr(message, "date", None), datetime):
                    next_offset = (cid, raw.top_message, utc(message.date))
                    next_peer = utils.get_input_peer(entities[cid])
                    break
            if next_offset is None or next_offset in builder.offsets:
                raise ToolError(
                    "CATALOG_ERROR",
                    "Telegram returned a dialog page without an advancing position.",
                )
            if len(builder.offsets) >= self.max_dialogs + 1:
                raise ToolError(
                    "CATALOG_ERROR", "Telegram returned too many pages without a complete catalog."
                )
        # No builder state changes until the whole response and its next offset are valid.
        builder.dialogs.update(additions)
        builder.self_id = self_id
        if finished:
            builder.phase = "filters"
        else:
            assert next_offset is not None
            builder.offsets.add(next_offset)
            builder.offset_id, builder.offset_date = next_offset[1:]
            builder.offset_peer = next_peer

    @staticmethod
    def _validate_dialogs(response: Any) -> None:
        if not all(
            isinstance(getattr(response, name, None), list)
            for name in ("dialogs", "messages", "users", "chats")
        ):
            raise ToolError("CATALOG_ERROR", "Telegram returned unusable dialog metadata.")

    def _collect_dialogs(
        self, builder: _CatalogBuilder, response: Any, *, archived: bool = False
    ) -> tuple[dict[str, _DialogView], str | None]:
        entities = {peer_id(entity): entity for entity in [*response.users, *response.chats]}
        additions: dict[str, _DialogView] = {}
        self_id = builder.self_id
        for raw in response.dialogs:
            # A folder row summarizes an archive; its peer is not an ordinary dialog.
            if isinstance(raw, types.DialogFolder):
                continue
            if not isinstance(raw, types.Dialog):
                raise ToolError("CATALOG_ERROR", "Telegram returned an unsupported dialog.")
            cid = peer_id(raw.peer)
            entity = entities.get(cid)
            if entity is None or isinstance(entity, (types.UserEmpty, types.ChatEmpty)):
                raise ToolError("CATALOG_ERROR", "Telegram omitted a dialog's entity metadata.")
            if getattr(entity, "is_self", False):
                self_id = cid
            if not getattr(entity, "migrated_to", None) and cid not in builder.dialogs:
                additions[cid] = _DialogView(entity, raw, archived)
                if len(builder.dialogs) + len(additions) > self.max_dialogs:
                    raise ToolError(
                        "CATALOG_LIMIT",
                        "Dialog catalog exceeds max_dialogs; increase it in local config.",
                    )
        return additions, self_id

    def _complete_catalog(self, builder: _CatalogBuilder) -> _CatalogSnapshot:
        now = self.clock.utc_now()
        defaults = {
            name: (until := getattr(notify, "mute_until", None)) is not None and utc(until) > now
            for name, notify in builder.defaults.items()
        }
        chats: list[Chat] = []
        for dialog in builder.dialogs.values():
            entity = dialog.entity
            category = (
                "private"
                if isinstance(entity, types.User)
                else "channel"
                if getattr(entity, "broadcast", False)
                else "group"
            )
            chats.append(chat_from_entity(entity, dialog, defaults.get(category, False), now=now))
        folders = [
            folder_from_tl(raw, index, builder.self_id)
            for index, raw in enumerate(builder.filters)
            if isinstance(raw, (types.DialogFilter, types.DialogFilterChatlist))
        ]
        return _CatalogSnapshot(chats, folders, self.clock.monotonic(), self.runtime.generation)

    async def resolve_entity(self, ref: str) -> Any:
        value = ref.strip()
        if value.startswith(("t.me/", "telegram.me/", "www.t.me/")):
            value = "https://" + value
        if value.startswith(("https://", "http://")):
            url = urlparse(value)
            if url.hostname not in ("t.me", "telegram.me", "www.t.me") or url.username:
                raise ToolError(
                    "INVALID_REFERENCE", "Use a Telegram chat ID, username or t.me link."
                )
            value = url.path.strip("/")
            if value.startswith("s/"):
                value = value[2:]
            if "/" in value or value.startswith("+"):
                raise ToolError(
                    "INVALID_REFERENCE", "Invite links and message links are not chat references."
                )
        target: int | str = int(value) if re.fullmatch(r"-?[1-9]\d*", value) else value
        if isinstance(target, str) and not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}|me", target):
            raise ToolError("INVALID_REFERENCE", "Use a numeric chat ID or exact username.")
        client = await self.runtime.ensure()
        try:
            return await self.runtime.call(client.get_entity(target))
        except ToolError as exc:
            if isinstance(target, int) and exc.code == "INVALID_REFERENCE":
                raise ToolError(
                    "INVALID_REFERENCE",
                    "Telegram has no entity for this numeric chat ID. Use its username or t.me "
                    "link first.",
                ) from None
            raise

    async def resolve_chat(self, ref: str, *, refresh: bool = False) -> Chat:
        if not refresh and ref.strip() in self.cache:
            return self.cache[ref.strip()]
        entity = await self.resolve_entity(ref)
        chat = chat_from_entity(entity, now=self.clock.utc_now())
        self.cache[chat.id] = chat
        return chat

    async def public_channel(self, chat_id: str) -> Chat:
        # get_entity performs a fresh GetChannels RPC for a known numeric entity;
        # the access hash may be cached, but public visibility must never be cached.
        entity = await self.resolve_entity(chat_id)
        if public_channel_from_entity(entity) is None:
            raise ToolError("ACCESS_DENIED", "This chat is not a public broadcast channel.")
        chat = chat_from_entity(entity, now=self.clock.utc_now())
        if chat.id != chat_id:
            raise ToolError("ACCESS_DENIED", "Telegram returned a different channel identity.")
        self.cache[chat.id] = chat
        return chat

    async def chat_details(self, chat: Chat) -> dict[str, object]:
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat.id)))
        if chat.type in ("private", "bot", "saved"):
            full = await self.runtime.call(client(functions.users.GetFullUserRequest(entity)))
            about = getattr(full.full_user, "about", None)
            linked = None
        elif isinstance(entity, types.InputPeerChannel):
            full = await self.runtime.call(client(functions.channels.GetFullChannelRequest(entity)))
            about = full.full_chat.about
            linked = getattr(full.full_chat, "linked_chat_id", None)
        else:
            full = await self.runtime.call(
                client(functions.messages.GetFullChatRequest(abs(int(chat.id))))
            )
            about = full.full_chat.about
            linked = None
        return {
            "description": about,
            "linked_chat_id": peer_id(types.PeerChannel(linked)) if linked else None,
            "server_can_send": False,
        }

    async def discussion_chat(self, chat: Chat) -> str:
        if chat.type == "group":
            return chat.id
        if chat.type != "channel":
            raise ToolError(
                "THREAD_UNAVAILABLE",
                "Telegram threads are available in supported groups and channels.",
            )
        details = await self.chat_details(chat)
        linked = details["linked_chat_id"]
        if not isinstance(linked, str):
            raise ToolError("THREAD_UNAVAILABLE", "Channel has no linked discussion group.")
        return linked
