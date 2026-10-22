from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.adapters.telegram.mapping import chat_from_entity, normalize_message
from telegram_mcp.application.requests import ReadFilters, TopicRead
from telegram_mcp.domain.models import MessageRef, ToolError
from tests.fakes import BASE
from tests.test_navigation_backend import CHAT_ID, forum, response, topic


def raw_message(
    mid: int,
    topic_id: int = 10,
    *,
    date: datetime | None = None,
    peer_channel_id: int = 123,
    sender_id: int | None = None,
) -> types.Message:
    return types.Message(
        id=mid,
        peer_id=types.PeerChannel(peer_channel_id),
        date=date or BASE + timedelta(seconds=mid),
        message=f"Message {mid}",
        from_id=types.PeerUser(sender_id) if sender_id is not None else None,
        reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic_id)
        if topic_id != 1
        else None,
    )


def message_page(
    messages: list[Any], *, count: int = 0, users: list[Any] | None = None
) -> types.messages.ChannelMessages:
    return types.messages.ChannelMessages(
        pts=1, count=count, messages=messages, topics=[], chats=[forum()], users=users or []
    )


def adapter(result: Any) -> tuple[TelegramBackend, AsyncMock]:
    client = AsyncMock(return_value=result)
    client.is_connected = lambda: True
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(123, 456))
    client._self_id = 999
    client._mb_entity_cache = SimpleNamespace(get=lambda _: None)
    settings = Settings()
    chat_cache = {}
    chat_cache[CHAT_ID] = chat_from_entity(forum(), now=BASE + timedelta(days=1))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    return backend, client


@pytest.mark.parametrize("topic_id", [1, 10])
async def test_get_topic_refreshes_exact_metadata_without_root_message_reads(topic_id: int) -> None:
    backend, client = adapter(response([topic(topic_id)], []))
    result = await backend.get_topic(CHAT_ID, topic_id)
    assert result.id == topic_id and result.chat_id == CHAT_ID
    assert result.is_general is (topic_id == 1)
    client.assert_awaited_once()
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.messages.GetForumTopicsByIDRequest)
    assert request.topics == [topic_id]
    assert request.peer == types.InputPeerChannel(123, 456)
    client.get_messages.assert_not_called()
    await backend.get_topic(CHAT_ID, topic_id)
    assert client.await_count == 2


async def test_get_topic_accepts_short_placeholder_peer_but_not_its_unknown_fields() -> None:
    short = topic(10, short=True)
    short.peer = types.PeerUser(0)
    backend, _ = adapter(response([short], []))
    result = await backend.get_topic(CHAT_ID, 10)
    assert result.id == 10 and result.is_short
    assert result.top_message_ref is None and result.is_pinned is None


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [types.ForumTopicDeleted(10)],
        [topic(11)],
        [topic(10, peer_channel_id=456)],
        [topic(10), types.ForumTopicDeleted(10)],
    ],
)
async def test_get_topic_reports_unavailable_without_claiming_deletion(rows: list[Any]) -> None:
    backend, _ = adapter(response(rows, []))
    with pytest.raises(ToolError) as caught:
        await backend.get_topic(CHAT_ID, 10)
    assert caught.value.code == "TOPIC_UNAVAILABLE"
    assert "deleted" not in caught.value.detail.lower()
    assert "456" not in caught.value.detail


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (errors.BadRequestError(request=None, message="TOPIC_ID_INVALID"), "TOPIC_UNAVAILABLE"),
        (errors.MsgIdInvalidError(request=None), "TOPIC_UNAVAILABLE"),
        (errors.TopicDeletedError(request=None), "TOPIC_UNAVAILABLE"),
        (errors.ChannelForumMissingError(request=None), "FORUM_UNAVAILABLE"),
        (errors.FloodWaitError(request=None, capture=13), "RATE_LIMITED"),
        (ConnectionError(), "CONNECTION_ERROR"),
    ],
)
async def test_get_topic_keeps_temporary_errors_distinct(failure: Exception, code: str) -> None:
    backend, client = adapter(None)
    client.side_effect = failure
    with pytest.raises(ToolError) as caught:
        await backend.get_topic(CHAT_ID, 10)
    assert caught.value.code == code


@pytest.mark.parametrize("topic_id", [0, -1, 2**31, True, "10", 10.5])
async def test_adapter_rejects_invalid_topic_id_before_requests(topic_id: Any) -> None:
    backend, client = adapter(None)
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await backend.get_topic(CHAT_ID, topic_id=topic_id)
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await backend.read(CHAT_ID, TopicRead(topic_id))
    client.assert_not_called()
    client.get_input_entity.assert_not_called()


@pytest.mark.parametrize("method", ["get_topic", "read"])
async def test_selected_topic_requires_a_supported_forum_before_rpc(method: str) -> None:
    backend, client = adapter(None)
    backend.chat_gateway.cache[CHAT_ID] = chat_from_entity(
        forum(enabled=False), now=BASE + timedelta(days=1)
    )
    with pytest.raises(ToolError, match="FORUM_UNAVAILABLE"):
        if method == "get_topic":
            await backend.get_topic(CHAT_ID, topic_id=10)
        else:
            await backend.read(CHAT_ID, TopicRead(10))
    client.assert_not_called()


@pytest.mark.parametrize("topic_id", [1, 10])
@pytest.mark.parametrize("oldest_first", [False, True])
async def test_history_uses_topic_replies_in_both_orders_and_continues_by_id(
    topic_id: int, oldest_first: bool
) -> None:
    backend, client = adapter(message_page([raw_message(mid, topic_id) for mid in [30, 20]]))
    batch = await backend.read(
        CHAT_ID,
        TopicRead(
            topic_id,
            filters=ReadFilters(
                oldest_first=oldest_first, date_from=BASE, date_to=BASE + timedelta(days=1)
            ),
        ),
        limit=7,
        offset=15 if oldest_first else 40,
    )
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.messages.GetRepliesRequest)
    assert request.msg_id == topic_id and request.peer == types.InputPeerChannel(123, 456)
    assert request.offset_id == (16 if oldest_first else 40)
    assert request.offset_date is None
    assert request.add_offset == (-7 if oldest_first else 0)
    assert request.limit == 7 and request.max_id == request.min_id == request.hash == 0
    assert [entry.message_ref.message_id for entry in batch.messages] == (
        [20, 30] if oldest_first else [30, 20]
    )
    assert batch.next_offset == (30 if oldest_first else 20)
    assert not batch.exhausted and not batch.issues
    client.assert_awaited_once()
    client.get_messages.assert_not_called()


@pytest.mark.parametrize("oldest_first", [False, True])
async def test_topic_history_first_page_broadens_date_offset_then_filters_half_open_range(
    oldest_first: bool,
) -> None:
    start, end = BASE + timedelta(seconds=20), BASE + timedelta(seconds=40)
    backend, client = adapter(message_page([raw_message(mid) for mid in [40, 30, 20, 19]]))
    batch = await backend.read(
        CHAT_ID,
        TopicRead(10, filters=ReadFilters(oldest_first=oldest_first, date_from=start, date_to=end)),
        limit=9,
    )
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert request.offset_id == 0
    assert request.offset_date == (
        start - timedelta(seconds=1) if oldest_first else end + timedelta(seconds=1)
    )
    assert [entry.message_ref.message_id for entry in batch.messages] == (
        [20, 30] if oldest_first else [30, 20]
    )
    assert batch.exhausted and not batch.issues


async def test_oldest_topic_history_without_lower_date_starts_at_first_message_id() -> None:
    backend, client = adapter(message_page([]))
    await backend.read(CHAT_ID, TopicRead(1, filters=ReadFilters(oldest_first=True)), limit=4)
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert request.offset_id == 1 and request.offset_date is None
    assert request.add_offset == -4


@pytest.mark.parametrize("topic_id", [1, 10])
async def test_scoped_pins_use_search_top_id_without_unfiltered_replies(topic_id: int) -> None:
    backend, client = adapter(message_page([raw_message(20, topic_id)]))
    end = BASE + timedelta(seconds=30)
    batch = await backend.read(
        CHAT_ID,
        TopicRead(topic_id, filters=ReadFilters(date_to=end), pinned_only=True),
        offset=25,
        limit=6,
    )
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.messages.SearchRequest)
    assert request.top_msg_id == topic_id and request.q == ""
    assert isinstance(request.filter, types.InputMessagesFilterPinned)
    assert request.offset_id == 25 and request.add_offset == 0 and request.limit == 6
    assert request.max_date == end + timedelta(seconds=1) and request.min_date is None
    assert request.max_id == request.min_id == request.hash == 0
    assert batch.messages[0].topic_id == topic_id and not batch.exhausted
    client.assert_awaited_once()
    client.get_messages.assert_not_called()


@pytest.mark.parametrize("selectors", [{"thread_id": 10}, {"query": "text"}, {"sender_id": "7"}])
async def test_selected_topic_does_not_silently_ignore_other_selectors(
    selectors: dict[str, Any],
) -> None:
    backend, client = adapter(None)
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await backend.read(CHAT_ID, TopicRead(10, **selectors))
    client.assert_not_called()


async def test_selected_page_normalizes_authors_media_unicode_entities_and_real_topic_root() -> (
    None
):
    root = types.MessageService(
        id=10,
        peer_id=types.PeerChannel(123),
        date=BASE + timedelta(seconds=10),
        action=types.MessageActionTopicCreate(title="Topic", icon_color=123),
        from_id=types.PeerUser(7),
    )
    raw = raw_message(30, sender_id=7)
    raw.message = "Привет 👩🏽‍💻 café"
    raw.edit_date = BASE + timedelta(seconds=31)
    raw.entities = [types.MessageEntityBold(offset=7, length=7)]
    raw.media = types.MessageMediaDocument(
        document=types.Document(
            id=1234,
            access_hash=567,
            file_reference=b"",
            date=BASE,
            mime_type="application/pdf",
            size=4567,
            dc_id=2,
            attributes=[types.DocumentAttributeFilename("guide.pdf")],
        )
    )
    raw.fwd_from = types.MessageFwdHeader(date=BASE, from_name="Hidden author")
    backend, client = adapter(
        message_page([raw, root], users=[types.User(id=7, first_name="Alice", access_hash=8)])
    )
    batch = await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()))
    entry, creation = batch.messages
    assert entry.sender.id == "7" and entry.sender.name == "Alice"
    assert entry.sender.type == "user"
    assert entry.text == raw.message and entry.text_total_length == len(raw.message)
    assert entry.entities[0].offset == 7 and entry.entities[0].length == 7
    assert entry.edited_at == raw.edit_date and entry.sent_at == raw.date
    assert entry.attachments[0].name == "guide.pdf" and entry.attachments[0].size == 4567
    assert entry.url == "https://t.me/example_forum/30"
    assert entry.forward_origin is not None and entry.forward_origin.name == "Hidden author"
    assert creation.topic_id == 10 and creation.content_type == "other"
    assert creation.message_ref.message_id == 10 and creation.thread_ref is None
    client.get_messages.assert_not_called()


@pytest.mark.parametrize(
    ("reply", "expected_topic", "expected_thread_chat", "expected_thread_id"),
    [
        (None, 1, None, None),
        (types.MessageReplyHeader(reply_to_msg_id=20), 1, None, None),
        (
            types.MessageReplyHeader(reply_to_msg_id=20, reply_to_top_id=15),
            1,
            CHAT_ID,
            15,
        ),
        (types.MessageReplyHeader(reply_to_top_id=1, reply_to_msg_id=5), 1, CHAT_ID, 1),
        (types.MessageReplyHeader(forum_topic=True, reply_to_top_id=1), 1, None, None),
        (types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=10), 10, None, None),
        (
            types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=999, reply_to_top_id=10),
            10,
            CHAT_ID,
            10,
        ),
        (
            types.MessageReplyHeader(
                forum_topic=True,
                reply_to_msg_id=77,
                reply_to_peer_id=types.PeerChannel(456),
                reply_to_top_id=10,
            ),
            10,
            CHAT_ID,
            10,
        ),
        (
            types.MessageReplyHeader(
                forum_topic=True, reply_to_msg_id=77, reply_to_peer_id=types.PeerChannel(456)
            ),
            None,
            None,
            None,
        ),
        (types.MessageReplyHeader(forum_topic=True), None, None, None),
        (
            types.MessageReplyHeader(reply_to_msg_id=77, reply_to_peer_id=types.PeerChannel(456)),
            1,
            None,
            None,
        ),
    ],
)
def test_topic_metadata_distinguishes_general_threads_and_foreign_reply_targets(
    reply: Any,
    expected_topic: int | None,
    expected_thread_chat: str | None,
    expected_thread_id: int | None,
) -> None:
    raw = raw_message(100)
    raw.reply_to = reply
    mapped = normalize_message(raw, chat_from_entity(forum(), now=BASE + timedelta(days=1)))
    assert mapped.topic_id == expected_topic
    expected_ref = (
        MessageRef(chat_id=expected_thread_chat, message_id=expected_thread_id)
        if expected_thread_chat and expected_thread_id
        else None
    )
    assert mapped.thread_ref == expected_ref
    if reply and reply.reply_to_peer_id:
        assert mapped.reply_to is not None and mapped.reply_to.chat_id == "-1000000000456"
    assert (
        normalize_message(
            raw, chat_from_entity(forum(enabled=False), now=BASE + timedelta(days=1))
        ).topic_id
        is None
    )


async def test_general_history_preserves_only_actual_general_messages_without_fake_root() -> None:
    direct_reply = raw_message(50, 1)
    direct_reply.reply_to = types.MessageReplyHeader(reply_to_msg_id=40, reply_to_top_id=30)
    backend, client = adapter(message_page([raw_message(60, 10), direct_reply, raw_message(20, 1)]))
    batch = await backend.read(CHAT_ID, TopicRead(1, filters=ReadFilters()))
    assert [entry.message_ref.message_id for entry in batch.messages] == [50, 20]
    assert all(entry.topic_id == 1 for entry in batch.messages)
    assert batch.messages[0].thread_ref == MessageRef(chat_id=CHAT_ID, message_id=30)
    assert batch.next_offset == 20 and not batch.exhausted
    assert [issue.code for issue in batch.issues] == ["PAGINATION_INCOMPLETE"]
    client.get_messages.assert_not_called()


async def test_topic_isolation_skips_foreign_and_ambiguous_rows_without_leaking_metadata() -> None:
    unknown = raw_message(70)
    unknown.reply_to = types.MessageReplyHeader(forum_topic=True)
    foreign = raw_message(99, peer_channel_id=456)
    foreign.message = "SECRET"
    backend, _ = adapter(message_page([foreign, unknown, raw_message(50, 11), raw_message(30)]))
    batch = await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()))
    assert [entry.message_ref.message_id for entry in batch.messages] == [30]
    assert batch.next_offset == 30 and not batch.exhausted
    assert len(batch.issues) == 2
    assert all(issue.chat_id == CHAT_ID for issue in batch.issues)
    assert "SECRET" not in str(batch) and "-1000000000456" not in str(batch)


@pytest.mark.parametrize("oldest_first", [False, True])
async def test_native_page_sorts_deduplicates_and_discards_previous_positions(
    oldest_first: bool,
) -> None:
    rows = [raw_message(mid) for mid in [20, 40, 30, 20, 60]]
    backend, _ = adapter(message_page(rows))
    batch = await backend.read(
        CHAT_ID,
        TopicRead(10, filters=ReadFilters(oldest_first=oldest_first)),
        offset=25 if oldest_first else 50,
    )
    assert [entry.message_ref.message_id for entry in batch.messages] == (
        [30, 40, 60] if oldest_first else [40, 30, 20]
    )
    assert batch.next_offset == (60 if oldest_first else 20)
    assert not batch.issues and not batch.exhausted


async def test_short_empty_placeholder_and_filtered_pages_preserve_positions() -> None:
    backend, client = adapter(message_page([raw_message(30)], count=1))
    first = await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()), limit=30)
    assert first.next_offset == 30 and not first.exhausted
    client.return_value = message_page([types.MessageEmpty(id=20, peer_id=None)])
    second = await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()), offset=30)
    assert not second.messages and second.next_offset == 20 and not second.exhausted
    assert not second.issues
    client.return_value = message_page([raw_message(15)])
    third = await backend.read(
        CHAT_ID, TopicRead(10, filters=ReadFilters(content_type="photo")), offset=20
    )
    assert not third.messages and third.next_offset == 15 and not third.exhausted
    client.return_value = message_page([], count=100)
    final = await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()), offset=15)
    assert final.exhausted and not final.messages and not final.issues
    assert final.next_offset == 15


@pytest.mark.parametrize(
    "rows",
    [
        [raw_message(30)],
        [raw_message(40)],
        [types.MessageEmpty(id=0, peer_id=None)],
        [raw_message(10, peer_channel_id=456)],
    ],
)
async def test_nonadvancing_pages_stop_with_incomplete_issue_instead_of_looping(
    rows: list[Any],
) -> None:
    backend, _ = adapter(message_page(rows))
    batch = await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()), offset=30)
    assert batch.exhausted and batch.next_offset == 30 and not batch.messages
    assert batch.issues and all(issue.code == "PAGINATION_INCOMPLETE" for issue in batch.issues)


async def test_unusable_response_does_not_falsely_report_complete_history() -> None:
    backend, _ = adapter(types.messages.MessagesNotModified(count=5))
    batch = await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()))
    assert batch.exhausted and not batch.messages
    assert [issue.code for issue in batch.issues] == ["PAGINATION_INCOMPLETE"]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (errors.TopicDeletedError(request=None), "TOPIC_UNAVAILABLE"),
        (errors.ChannelForumMissingError(request=None), "FORUM_UNAVAILABLE"),
    ],
)
async def test_topic_deleted_or_forum_changed_after_preflight_is_not_empty_success(
    failure: Exception, code: str
) -> None:
    backend, client = adapter(None)
    client.side_effect = failure
    with pytest.raises(ToolError) as caught:
        await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()))
    assert caught.value.code == code and not caught.value.retryable
    client.assert_awaited_once()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (errors.FloodWaitError(request=None, capture=15), "RATE_LIMITED"),
        (errors.ServerError(request=None, message="INTERNAL", code=500), "TELEGRAM_UNAVAILABLE"),
        (ConnectionError(), "CONNECTION_ERROR"),
    ],
)
async def test_topic_history_preserves_retryable_errors_without_internal_retries(
    failure: Exception, code: str
) -> None:
    backend, client = adapter(None)
    client.side_effect = failure
    with pytest.raises(ToolError) as caught:
        await backend.read(CHAT_ID, TopicRead(10, filters=ReadFilters()))
    assert caught.value.code == code and caught.value.retryable
    client.assert_awaited_once()
