"""Native text drafts with conservative handling of unsupported Telegram content."""

import hashlib
from typing import Any

from telethon import functions, types

from telegram_mcp.adapters.telegram.mapping import peer_id, utc
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.auth_notifications import authentication_notice
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.outbound import Draft


def normalize_draft(chat: str, raw: Any) -> Draft:
    if raw is None or isinstance(raw, types.DraftMessageEmpty):
        return Draft(chat_id=chat, revision=hashlib.sha256(b"empty").hexdigest())
    if authentication_notice(str(raw)):
        raise ToolError(
            "AUTH_NOTIFICATION_FILTERED", "This draft contains an authentication notice."
        )
    # Hash every field, including reply targets and media references, not just visible text.
    revision = hashlib.sha256(bytes(raw)).hexdigest()
    supported = isinstance(raw, types.DraftMessage) and not any(
        getattr(raw, field, None) for field in ("media", "rich_message", "suggested_post", "effect")
    )
    return Draft(
        chat_id=chat,
        text=getattr(raw, "message", ""),
        has_media=getattr(raw, "media", None) is not None,
        media_identity=media_identity(getattr(raw, "media", None)),
        revision=revision,
        updated_at=utc(raw.date) if raw.date else None,
        exists=True,
        supported=supported,
    )


def media_identity(media: Any) -> str | None:
    if isinstance(media, types.InputMediaDocument):
        return f"document:{media.id.id}"
    if isinstance(media, types.InputMediaPhoto):
        return f"photo:{media.id.id}"
    return None


class DraftGateway:
    def __init__(self, runtime: TelegramRuntime):
        self.runtime = runtime

    async def get(self, chat: str) -> Draft:
        client = await self.runtime.ensure()
        peer = await self.runtime.call(client.get_input_entity(int(chat)))
        response = await self.runtime.call(
            client(
                functions.messages.GetPeerDialogsRequest(
                    peers=[types.InputDialogPeer(peer)],
                )
            )
        )
        raw = next(
            (
                dialog.draft
                for dialog in response.dialogs
                if isinstance(dialog, types.Dialog) and peer_id(dialog.peer) == chat
            ),
            None,
        )
        return normalize_draft(chat, raw)

    async def save(self, chat: str, text: str) -> None:
        client = await self.runtime.ensure()
        peer = await self.runtime.call(client.get_input_entity(int(chat)))
        await self.runtime.write_call(
            client(
                functions.messages.SaveDraftRequest(
                    peer=peer,
                    message=text,
                    no_webpage=True,
                )
            ),
            scope=f"draft:{chat}",
        )
