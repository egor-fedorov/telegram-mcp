from datetime import timedelta
from typing import Any

from telethon import types

from tests.support.fakes import BASE


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
