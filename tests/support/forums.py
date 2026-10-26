from datetime import datetime

from telethon import types

from tests.support.fakes import BASE

CHAT_ID = "-1000000000123"


def forum(
    *,
    enabled: bool = True,
    megagroup: bool = True,
    broadcast: bool = False,
    monoforum: bool = False,
    view_banned: bool = False,
    username: str | None = "example_forum",
) -> types.Channel:
    return types.Channel(
        id=123,
        title="Forum",
        date=BASE,
        photo=types.ChatPhotoEmpty(),
        access_hash=456,
        forum=enabled,
        megagroup=megagroup,
        broadcast=broadcast,
        monoforum=monoforum,
        username=username,
        banned_rights=types.ChatBannedRights(until_date=None, view_messages=view_banned),
    )


def topic(
    topic_id: int,
    *,
    top_message: int = 100,
    date: datetime | None = BASE,
    short: bool = False,
    peer_channel_id: int = 123,
) -> types.ForumTopic:
    return types.ForumTopic(
        id=topic_id,
        date=date,
        peer=types.PeerChannel(peer_channel_id),
        title=f"Topic {topic_id}",
        icon_color=0x6FB9F0,
        icon_emoji_id=987654,
        top_message=top_message,
        read_inbox_max_id=90,
        read_outbox_max_id=90,
        unread_count=3,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(7),
        notify_settings=types.PeerNotifySettings(),
        short=short,
        closed=True,
        hidden=True,
        pinned=True,
    )


def response(
    topics: list[types.TypeForumTopic],
    messages: list[types.TypeMessage],
    *,
    order_by_create_date: bool = False,
    count: int = 100,
) -> types.messages.ForumTopics:
    return types.messages.ForumTopics(
        count=count,
        topics=topics,
        messages=messages,
        chats=[],
        users=[],
        pts=1,
        order_by_create_date=order_by_create_date,
    )
