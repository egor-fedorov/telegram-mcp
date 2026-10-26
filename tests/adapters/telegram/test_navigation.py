from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.adapters.telegram.mapping import chat_from_entity
from telegram_mcp.application.ports import TopicOffset
from telegram_mcp.application.requests import PinnedRead, ReadFilters
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import BASE
from tests.support.forums import CHAT_ID, forum, response, topic


def message(
    message_id: int = 100, *, date: datetime | None = BASE, peer_channel_id: int = 123
) -> types.Message:
    return types.Message(
        id=message_id, date=date, peer_id=types.PeerChannel(peer_channel_id), message="Post"
    )


def adapter(result: types.messages.ForumTopics) -> tuple[TelegramBackend, AsyncMock]:
    client = AsyncMock(return_value=result)
    client.is_connected = lambda: True
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(123, 456))
    settings = Settings()
    chat_cache = {}
    chat_cache[CHAT_ID] = chat_from_entity(forum(), now=BASE + timedelta(days=1))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    return backend, client


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (forum(), True),
        (forum(enabled=False), False),
        (forum(megagroup=False), False),
        (forum(broadcast=True), False),
        (forum(monoforum=True), False),
        (forum(view_banned=True), False),
        (types.User(id=123, bot=True), False),
        (types.ChatForbidden(id=123, title="Group"), False),
        (types.ChannelForbidden(id=123, access_hash=1, title="Forbidden", megagroup=True), False),
    ],
)
def test_forum_flag_requires_supported_raw_supergroup(entity: object, expected: bool) -> None:
    assert chat_from_entity(entity, now=BASE + timedelta(days=1)).is_forum is expected


async def test_topics_use_messages_namespace_and_exact_offsets_without_detail_reads() -> None:
    backend, client = adapter(response([topic(7)], [message()]))
    offset = TopicOffset(BASE - timedelta(days=1), 22, 3)
    result = await backend.fetch_topics(CHAT_ID, offset=offset, limit=17, query="work")
    client.assert_awaited_once()
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.messages.GetForumTopicsRequest)
    assert isinstance(request.peer, types.InputPeerChannel)
    assert request.peer.channel_id == 123
    assert request.offset_date == offset.date
    assert request.offset_id == 22 and request.offset_topic == 3
    assert request.limit == 17 and request.q == "work"
    assert result.next_offset == TopicOffset(BASE, 100, 7)
    assert not result.exhausted and result.telegram_total_count == 100
    client.get_messages.assert_not_called()
    client.get_entity.assert_not_called()
    client.get_dialogs.assert_not_called()


@pytest.mark.parametrize("creation_order", [False, True])
async def test_topic_offsets_use_exact_tail_and_matching_message_peer(creation_order: bool) -> None:
    created = BASE - timedelta(days=5)
    sent = BASE + timedelta(hours=1)
    backend, _ = adapter(
        response(
            [topic(7, top_message=100), topic(8, top_message=200, date=created)],
            [
                message(200, date=BASE + timedelta(days=50), peer_channel_id=999),
                message(200, date=sent),
                message(100, date=BASE + timedelta(days=10)),
            ],
            order_by_create_date=creation_order,
            count=2,
        )
    )
    batch = await backend.fetch_topics(CHAT_ID, offset=TopicOffset(), limit=30)
    assert batch.next_offset == TopicOffset(created if creation_order else sent, 200, 8)
    assert batch.order_by_create_date is creation_order
    assert not batch.exhausted and not batch.issues


@pytest.mark.parametrize("username", [None, "example_forum"])
async def test_topic_models_preserve_general_deleted_and_short_metadata(
    username: str | None,
) -> None:
    backend, _ = adapter(
        response(
            [topic(1), types.ForumTopicDeleted(8), topic(9, short=True), topic(10)], [message()]
        )
    )
    backend.chat_gateway.cache[CHAT_ID] = chat_from_entity(
        forum(username=username), now=BASE + timedelta(days=1)
    )
    batch = await backend.fetch_topics(CHAT_ID, offset=TopicOffset())
    general, deleted, short, full = batch.topics
    assert general.is_general and general.root_message_ref is None
    assert general.top_message_ref is not None and general.top_message_ref.message_id == 100
    assert general.url is None
    assert deleted.is_deleted and deleted.id == 8 and deleted.chat_id == CHAT_ID
    assert deleted.title is None and deleted.created_at is None
    assert deleted.root_message_ref is None and deleted.top_message_ref is None
    assert deleted.is_closed is None and deleted.is_pinned is None and deleted.url is None
    assert short.is_short and short.is_closed
    assert short.created_at == BASE and short.title == "Topic 9"
    assert short.root_message_ref is not None and short.root_message_ref.message_id == 9
    assert short.is_hidden is None and short.is_pinned is None
    assert short.unread_count is None and short.top_message_ref is None
    assert full.root_message_ref is not None and full.root_message_ref.message_id == 10
    assert full.top_message_ref is not None and full.top_message_ref.message_id == 100
    assert full.is_pinned and full.is_hidden and full.is_closed
    assert full.unread_count == 3 and full.icon_color == 0x6FB9F0
    assert full.icon_emoji_id == "987654"
    assert full.url == f"https://t.me/{username or 'c/123'}/10"


@pytest.mark.parametrize("short_is_tail", [False, True])
async def test_short_topic_placeholder_peer_is_bound_to_requested_chat(
    short_is_tail: bool,
) -> None:
    short = topic(7, short=True)
    short.peer = types.PeerUser(0)
    rows: list[types.TypeForumTopic] = [topic(8), short] if short_is_tail else [short, topic(8)]
    backend, _ = adapter(response(rows, [message()]))
    batch = await backend.fetch_topics(CHAT_ID, offset=TopicOffset())
    assert [item.id for item in batch.topics] == [row.id for row in rows]
    normalized = next(item for item in batch.topics if item.id == 7)
    assert normalized.chat_id == CHAT_ID and normalized.is_short
    assert normalized.title == "Topic 7" and normalized.is_closed
    assert normalized.root_message_ref is not None
    assert normalized.root_message_ref.chat_id == CHAT_ID
    assert normalized.top_message_ref is None and normalized.unread_count is None
    assert normalized.is_hidden is None and normalized.is_pinned is None
    if short_is_tail:
        assert batch.next_offset is None and batch.exhausted
        assert [issue.code for issue in batch.issues] == ["PAGINATION_INCOMPLETE"]
    else:
        assert batch.next_offset == TopicOffset(BASE, 100, 8) and not batch.exhausted
        assert not batch.issues


async def test_short_nonempty_topic_page_does_not_exhaust_until_native_empty_page() -> None:
    backend, client = adapter(response([topic(7)], [message()], count=1))
    first = await backend.fetch_topics(CHAT_ID, offset=TopicOffset(), limit=100)
    assert not first.exhausted and first.next_offset is not None
    client.return_value = response([], [], count=1)
    last = await backend.fetch_topics(CHAT_ID, offset=first.next_offset, limit=100)
    assert last.exhausted and last.next_offset is None and last.topics == []
    assert not last.issues


@pytest.mark.parametrize(
    ("tail", "messages", "creation_order"),
    [
        (types.ForumTopicDeleted(8), [message()], False),
        (topic(8, short=True), [message()], True),
        (topic(8, date=None), [message()], True),
        (topic(8), [], False),
        (topic(8), [message(date=None)], False),
        (topic(8), [message(peer_channel_id=999)], False),
        (topic(8), [types.MessageEmpty(id=100, peer_id=types.PeerChannel(123))], False),
        (topic(8, peer_channel_id=999), [message()], False),
    ],
)
async def test_unsafe_exact_tail_reports_incomplete_without_guessing_offset(
    tail: types.TypeForumTopic, messages: list[types.TypeMessage], creation_order: bool
) -> None:
    backend, _ = adapter(response([topic(7), tail], messages, order_by_create_date=creation_order))
    batch = await backend.fetch_topics(CHAT_ID, offset=TopicOffset())
    assert batch.topics[0].id == 7
    assert batch.exhausted and batch.next_offset is None
    assert batch.issues and all(issue.code == "PAGINATION_INCOMPLETE" for issue in batch.issues)
    assert all(issue.chat_id == CHAT_ID for issue in batch.issues)


async def test_foreign_topic_is_not_exposed_but_safe_tail_can_continue() -> None:
    backend, _ = adapter(response([topic(99, peer_channel_id=999), topic(7)], [message()]))
    batch = await backend.fetch_topics(CHAT_ID, offset=TopicOffset())
    assert [entry.id for entry in batch.topics] == [7]
    assert batch.next_offset == TopicOffset(BASE, 100, 7) and not batch.exhausted
    assert [issue.code for issue in batch.issues] == ["PAGINATION_INCOMPLETE"]
    assert "Topic 99" not in str(batch.issues) and "-1000000000999" not in str(batch.issues)


@pytest.mark.parametrize(
    "entity", [forum(enabled=False), forum(monoforum=True), forum(view_banned=True)]
)
async def test_unsupported_forums_fail_before_topic_rpc(entity: types.Channel) -> None:
    client = AsyncMock()
    client.is_connected = lambda: True
    client.get_entity = AsyncMock(return_value=entity)
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    with pytest.raises(ToolError, match="FORUM_UNAVAILABLE"):
        await backend.fetch_topics(CHAT_ID, offset=TopicOffset())
    client.assert_not_called()
    client.get_input_entity.assert_not_called()


async def test_chat_wide_pins_use_filter_with_existing_message_normalization() -> None:
    backend, client = adapter(response([], []))
    client.get_messages = AsyncMock(return_value=[message(20)])
    batch = await backend.read(CHAT_ID, PinnedRead(filters=ReadFilters()), offset=30, limit=5)
    client.get_messages.assert_awaited_once()
    assert client.get_messages.await_args is not None
    args = client.get_messages.await_args
    assert args.args == (int(CHAT_ID),)
    assert args.kwargs["offset_id"] == 30 and args.kwargs["limit"] == 5
    assert isinstance(args.kwargs["filter"], types.InputMessagesFilterPinned)
    assert "reply_to" not in args.kwargs
    assert [entry.message_ref.message_id for entry in batch.messages] == [20]
    assert batch.messages[0].url == "https://t.me/example_forum/20"
    assert batch.next_offset == 20 and batch.exhausted


async def test_pins_cannot_accidentally_use_unfiltered_thread_replies() -> None:
    backend, client = adapter(response([], []))
    unsupported: dict[str, Any] = {"thread_id": 7}
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await backend.read(CHAT_ID, PinnedRead(**unsupported))
    client.get_messages.assert_not_called()
