from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.application.requests import HistoryRead, ReadFilters, SearchRead
from telegram_mcp.domain.models import Chat, MessageRef, ToolError
from tests.support.fakes import BASE


async def test_link_resolution_and_rejection_of_external_links() -> None:
    backend = TelegramBackend(Settings())
    backend.chat_gateway.cache["-1000000000123"] = Chat(
        id="-1000000000123", title="News", type="channel"
    )
    assert await backend.resolve_message("https://t.me/c/123/44") == MessageRef(
        chat_id="-1000000000123", message_id=44
    )
    assert await backend.resolve_message("https://t.me/c/123/9/44") == MessageRef(
        chat_id="-1000000000123", message_id=44
    )
    assert await backend.resolve_message("12:44") == MessageRef(chat_id="12", message_id=44)
    with pytest.raises(ToolError, match="INVALID_REFERENCE"):
        await backend.resolve_message("https://example.com/123/44")
    with pytest.raises(ToolError, match="INVALID_REFERENCE"):
        await backend.resolve_message("https://t.me/+invite")


async def test_real_adapter_filters_dates_without_losing_pagination_position() -> None:
    settings = Settings()
    chat_cache = {}
    chat_cache["1"] = Chat(id="1", title="Friend", type="private")
    raw = [
        types.Message(
            id=i, peer_id=types.PeerUser(1), date=BASE + timedelta(minutes=i), message="budget"
        )
        for i in (4, 3, 2, 1)
    ]
    client = AsyncMock(
        return_value=types.messages.Messages(messages=list(raw), users=[], chats=[], topics=[])
    )
    client.is_connected = lambda: True
    client.get_input_entity.return_value = types.InputPeerUser(1, 2)
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    result = await backend.read(
        "1",
        SearchRead(
            "budget",
            filters=ReadFilters(
                date_from=BASE + timedelta(minutes=2), date_to=BASE + timedelta(minutes=4)
            ),
        ),
        limit=4,
    )
    assert [m.message_ref.message_id for m in result.messages] == [3, 2]
    assert result.next_offset == 1 and result.exhausted
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.messages.SearchRequest) and request.q == "budget"
    assert request.max_date > BASE + timedelta(minutes=4)
    client.assert_awaited_once()


async def test_comment_links_resolve_linked_group_before_fetching_a_message() -> None:
    backend = TelegramBackend(Settings())
    backend.chat_gateway.cache["-1000000000123"] = Chat(
        id="-1000000000123", title="News", type="channel"
    )
    backend.chat_gateway.discussion_chat = AsyncMock(return_value="-1000000000456")
    ref = await backend.resolve_message("https://t.me/c/123/10?comment=27")
    assert ref == MessageRef(chat_id="-1000000000456", message_id=27)


async def test_discussion_root_uses_actual_group_message_id() -> None:
    settings = Settings()
    chat_cache = {}
    group_root = types.Message(id=87, peer_id=types.PeerChannel(456), date=BASE, message="post")
    response = SimpleNamespace(messages=[group_root])
    client = AsyncMock(return_value=response)
    client.is_connected = lambda: True
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(123, 1))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    root = await backend.thread_root(
        MessageRef(chat_id="-1000000000123", message_id=10), "-1000000000456"
    )
    assert root == MessageRef(chat_id="-1000000000456", message_id=87)


async def test_batch_lookup_deduplicates_and_retains_missing_ids_and_metadata() -> None:
    settings = Settings()
    chat_cache = {}
    chat_cache["-1000000000123"] = Chat(
        id="-1000000000123", title="News", type="channel", username="example"
    )
    message = types.Message(
        id=10,
        peer_id=types.PeerChannel(123),
        date=BASE,
        edit_date=BASE + timedelta(seconds=1),
        message="😀bold",
        entities=[types.MessageEntityBold(offset=2, length=4)],
    )
    client = SimpleNamespace(
        is_connected=lambda: True,
        get_messages=AsyncMock(
            return_value=[None, message, types.MessageEmpty(id=12, peer_id=types.PeerChannel(123))]
        ),
    )
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    results = await backend.messages("-1000000000123", [11, 10, 10, 12])
    client.get_messages.assert_awaited_once_with(-1000000000123, ids=[11, 10, 12])
    assert results[11] is None and results[12] is None
    found = results[10]
    assert found is not None
    assert found.text == "😀bold" and found.text_total_length == 5
    assert found.entities[0].offset == 2 and found.entities[0].length == 4
    assert found.sent_at == BASE and found.edited_at == BASE + timedelta(seconds=1)
    assert found.url == "https://t.me/example/10"
    assert await backend.messages("-1000000000123", []) == {}
    assert client.get_messages.await_count == 1
    client.get_messages.return_value = [message]
    assert await backend.message(MessageRef(chat_id="-1000000000123", message_id=10)) == found


@pytest.mark.parametrize("oldest_first", [False, True])
async def test_date_boundaries_and_offsets_across_adapter_pages(oldest_first: bool) -> None:
    settings = Settings()
    chat_cache = {}
    chat_cache["1"] = Chat(id="1", title="Friend", type="private")
    ids = [1, 2, 3, 4] if oldest_first else [4, 3, 2, 1]
    raw = [
        types.Message(
            id=i, peer_id=types.PeerUser(1), date=BASE + timedelta(seconds=i), message="x"
        )
        for i in ids
    ]
    client = SimpleNamespace(
        is_connected=lambda: True, get_messages=AsyncMock(side_effect=[raw[:2], raw[2:]])
    )
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    first = await backend.read(
        "1",
        HistoryRead(
            filters=ReadFilters(
                date_from=BASE + timedelta(seconds=2),
                date_to=BASE + timedelta(seconds=4),
                oldest_first=oldest_first,
            )
        ),
        limit=2,
    )
    second = await backend.read(
        "1",
        HistoryRead(
            filters=ReadFilters(
                date_from=BASE + timedelta(seconds=2),
                date_to=BASE + timedelta(seconds=4),
                oldest_first=oldest_first,
            )
        ),
        offset=first.next_offset,
        limit=2,
    )
    assert [m.message_ref.message_id for m in first.messages + second.messages] == (
        [2, 3] if oldest_first else [3, 2]
    )
    assert not first.exhausted and second.exhausted
    assert first.next_offset == ids[1] and second.next_offset == ids[-1]
    calls = client.get_messages.await_args_list
    assert calls[0].kwargs["offset_date"] == BASE + timedelta(seconds=1 if oldest_first else 5)
    assert "offset_date" not in calls[1].kwargs
    assert calls[1].kwargs["offset_id"] == ids[1]


async def test_empty_telegram_messages_advance_pagination() -> None:
    settings = Settings()
    chat_cache = {}
    chat_cache["1"] = Chat(id="1", title="Friend", type="private")
    backend = TelegramBackend(
        settings,
        runtime=TelegramRuntime(
            settings,
            client=SimpleNamespace(
                is_connected=lambda: True,
                get_messages=AsyncMock(
                    return_value=[
                        types.MessageEmpty(id=i, peer_id=types.PeerUser(1)) for i in (20, 19)
                    ]
                ),
            ),
        ),
        chat_cache=chat_cache,
    )
    page = await backend.read("1", HistoryRead(filters=ReadFilters()), limit=2)
    assert page.messages == [] and page.next_offset == 19 and not page.exhausted
