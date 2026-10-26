import inspect
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from telethon import functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from tests.support.fakes import BASE, FakeClock


def dialog_page(ids: list[int], *, sliced: bool = False):
    rows = [
        types.Dialog(
            peer=types.PeerUser(mid),
            top_message=mid,
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=1,
            unread_mentions_count=0,
            unread_reactions_count=0,
            unread_poll_votes_count=0,
            notify_settings=types.PeerNotifySettings(),
        )
        for mid in ids
    ]
    data: dict[str, Any] = dict(
        dialogs=rows,
        users=[types.User(id=mid, access_hash=mid, first_name=f"User {mid}") for mid in ids],
        chats=[],
        messages=[
            types.Message(
                id=mid, peer_id=types.PeerUser(mid), date=BASE + timedelta(seconds=mid), message=""
            )
            for mid in ids
        ],
    )
    return (
        types.messages.DialogsSlice(count=2000, **data)
        if sliced
        else types.messages.Dialogs(**data)
    )


def pinned_page(ids: list[int]):
    page = dialog_page(ids)
    return types.messages.PeerDialogs(
        dialogs=page.dialogs,
        messages=page.messages,
        users=page.users,
        chats=page.chats,
        state=types.updates.State(pts=0, qts=0, date=BASE, seq=0, unread_count=0),
    )


def gateway(respond, *, ttl: float = 300, stale: float = 300, maximum: int = 10000, folders=None):
    clock = FakeClock(now=BASE)

    async def with_pinned(request):
        if isinstance(request, functions.updates.GetStateRequest):
            return None
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return SimpleNamespace(filters=folders() if callable(folders) else folders or [])
        if isinstance(request, functions.messages.GetPinnedDialogsRequest):
            return pinned_page([])
        result = respond(request)
        return await result if inspect.isawaitable(result) else result

    client = AsyncMock(side_effect=with_pinned)
    client.is_connected = Mock(return_value=True)
    runtime = TelegramRuntime(Settings(), client=client, clock=clock)
    chats = ChatGateway(
        runtime,
        clock=clock,
        max_dialogs=maximum,
        catalog_cache_ttl_seconds=ttl,
        catalog_cache_stale_seconds=stale,
    )
    return chats, client, clock


def simple_response(request):
    if isinstance(request, functions.messages.GetPinnedDialogsRequest):
        return pinned_page([])
    if isinstance(request, functions.updates.GetStateRequest):
        return None
    if isinstance(request, functions.messages.GetDialogFiltersRequest):
        return SimpleNamespace(filters=[])
    assert isinstance(request, functions.messages.GetDialogsRequest)
    return dialog_page([1])
