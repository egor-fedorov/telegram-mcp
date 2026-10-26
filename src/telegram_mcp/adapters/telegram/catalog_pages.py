"""Validate and resume bounded raw dialog pages without folder-policy decisions."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from telethon import functions, types, utils

from telegram_mcp.adapters.telegram.mapping import peer_id, utc
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.models import ToolError


@dataclass
class DialogView:
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
class DialogScan:
    last_used: float
    auth_generation: int
    dialogs: dict[str, DialogView] = field(default_factory=dict)
    phase: str = "pinned"
    pinned_folder_id: int = 0
    offset_date: datetime | None = None
    offset_id: int = 0
    offset_peer: Any = field(default_factory=types.InputPeerEmpty)
    offsets: set[tuple[str, int, datetime]] = field(default_factory=set)
    defaults: dict[str, Any] = field(default_factory=dict)
    self_id: str | None = None


class DialogScanner:
    def __init__(self, runtime: TelegramRuntime, max_dialogs: int):
        self.runtime, self.max_dialogs = runtime, max_dialogs

    async def step(self, client: Any, builder: DialogScan) -> None:
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

    async def _dialog_page(self, client: Any, builder: DialogScan) -> None:
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
            builder.phase = "complete"
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
        self, builder: DialogScan, response: Any, *, archived: bool = False
    ) -> tuple[dict[str, DialogView], str | None]:
        entities = {peer_id(entity): entity for entity in [*response.users, *response.chats]}
        additions: dict[str, DialogView] = {}
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
                additions[cid] = DialogView(entity, raw, archived)
                if len(builder.dialogs) + len(additions) > self.max_dialogs:
                    raise ToolError(
                        "CATALOG_LIMIT",
                        "Dialog catalog exceeds max_dialogs; increase it in local config.",
                    )
        return additions, self_id
