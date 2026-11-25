"""Evaluate write and read folder grants using fresh metadata for one selected dialog."""

from telethon import functions, types
from telethon.tl.custom import Dialog

from telegram_mcp.adapters.telegram.mapping import chat_from_entity, folder_from_tl, peer_id, utc
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.domain.folders import contains
from telegram_mcp.domain.models import ToolError


async def action_folders(
    runtime: TelegramRuntime, chat_id: str, account: str, wanted: set[int]
) -> set[int]:
    client = await runtime.ensure()
    result = await runtime.call(client(functions.messages.GetDialogFiltersRequest()))
    filters = [
        folder_from_tl(raw, i, account)
        for i, raw in enumerate(result.filters)
        if isinstance(raw, (types.DialogFilter, types.DialogFilterChatlist)) and raw.id in wanted
    ]
    if not filters:
        return set()
    peer = await runtime.call(client.get_input_entity(int(chat_id)))
    result = await runtime.call(
        client(functions.messages.GetPeerDialogsRequest([types.InputDialogPeer(peer)]))
    )
    raw_dialog = next(
        (d for d in result.dialogs if isinstance(d, types.Dialog) and peer_id(d.peer) == chat_id),
        None,
    )
    if raw_dialog is None:
        return set()
    entities = {int(peer_id(e)): e for e in [*result.users, *result.chats]}
    entity = entities.get(int(chat_id))
    if not isinstance(entity, (types.Chat, types.User, types.Channel)) or getattr(
        entity, "min", False
    ):
        raise ToolError("CATALOG_ERROR", "Telegram returned incomplete dialog metadata.")
    now = runtime.clock.utc_now()
    default_muted = False
    if raw_dialog.notify_settings.mute_until is None and any(f.exclude_muted for f in filters):
        category = (
            types.InputNotifyUsers()
            if isinstance(entity, types.User)
            else types.InputNotifyBroadcasts()
            if getattr(entity, "broadcast", False)
            else types.InputNotifyChats()
        )
        notify = await runtime.call(client(functions.account.GetNotifySettingsRequest(category)))
        default_muted = notify.mute_until is not None and utc(notify.mute_until) > now
    chat = chat_from_entity(
        entity, Dialog(client, raw_dialog, entities, None), default_muted, now=now
    )
    return {f.id for f in filters if contains(f, chat)}
