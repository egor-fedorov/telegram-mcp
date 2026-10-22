from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.application.ports import GlobalOffset, SearchAnchor
from telegram_mcp.application.requests import ReadFilters, SearchRead
from telegram_mcp.domain.models import Chat, ToolError
from tests.fakes import BASE

CHAT = Chat(id="-1000000000123", title="News", type="channel", username="news")


def row(mid: int, *, cid: int = 123, second: int | None = None, **kwargs: Any) -> types.Message:
    return types.Message(
        id=mid,
        peer_id=types.PeerChannel(cid),
        date=BASE + timedelta(seconds=mid if second is None else second),
        message=f"Post {mid}",
        **kwargs,
    )


def channel(cid: int = 123) -> types.Channel:
    return types.Channel(
        id=cid,
        title="News",
        photo=types.ChatPhotoEmpty(),
        date=BASE,
        access_hash=9000 + cid,
        broadcast=True,
    )


def page(rows: list[Any], *, full: bool = False, **kwargs: Any) -> Any:
    values: dict[str, Any] = {
        "messages": rows,
        "users": [types.User(id=42, first_name="Ada")],
        "chats": [channel(), channel(456)],
        "topics": [],
    }
    return (
        types.messages.Messages(**values)
        if full
        else types.messages.MessagesSlice(count=999, **values, **kwargs)
    )


def adapter(response: Any) -> tuple[TelegramBackend, AsyncMock]:
    client = AsyncMock(return_value=response)
    client.is_connected = lambda: True
    client.get_input_entity.return_value = types.InputPeerChannel(123, 9123)
    settings = Settings()
    return TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache={CHAT.id: CHAT}
    ), client


def last_request(client: AsyncMock) -> Any:
    assert client.await_args is not None
    return client.await_args.args[0]


@pytest.mark.parametrize(
    "content,has_link,kind",
    [
        ("photo", None, types.InputMessagesFilterPhotos),
        ("voice", None, types.InputMessagesFilterVoice),
        ("audio", None, types.InputMessagesFilterMusic),
        ("video", None, types.InputMessagesFilterEmpty),
        ("document", None, types.InputMessagesFilterEmpty),
        ("text", None, types.InputMessagesFilterEmpty),
        ("photo", True, types.InputMessagesFilterUrl),
        (None, False, types.InputMessagesFilterEmpty),
    ],
)
async def test_search_uses_one_raw_page_and_safe_native_filter(
    content: Any, has_link: bool | None, kind: Any
) -> None:
    backend, client = adapter(page([row(10)]))
    result = await backend.read(
        CHAT.id, SearchRead("Post", ReadFilters(content_type=content, has_link=has_link))
    )
    client.assert_awaited_once()
    request = last_request(client)
    assert isinstance(request, functions.messages.SearchRequest) and isinstance(
        request.filter, kind
    )
    assert request.q == "Post" and request.limit == 30
    assert result.next_offset == 10 and not result.exhausted
    client.get_messages.assert_not_called()


@pytest.mark.parametrize("oldest", [False, True])
async def test_filter_only_date_anchor_is_a_separate_budgeted_page(oldest: bool) -> None:
    anchor_id = 9 if oldest else 19
    backend, client = adapter(page([row(anchor_id)]))
    filters = ReadFilters(
        date_from=BASE + timedelta(seconds=10),
        date_to=BASE + timedelta(seconds=20),
        oldest_first=oldest,
        has_link=True,
    )
    request = SearchRead("", filters)
    first = await backend.read(CHAT.id, request)
    assert (
        first.messages == []
        and first.next_offset == SearchAnchor(anchor_id + 1)
        and not first.exhausted
    )
    assert isinstance(last_request(client), functions.messages.GetHistoryRequest)
    client.assert_awaited_once()
    client.return_value = page([row(12, entities=[types.MessageEntityUrl(0, 4)])])
    second = await backend.read(CHAT.id, request, offset=first.next_offset)
    assert isinstance(last_request(client), functions.messages.SearchRequest)
    assert last_request(client).offset_id == anchor_id + 1
    assert [item.message_ref.message_id for item in second.messages] == [12]
    assert client.await_count == 2


@pytest.mark.parametrize("oldest", [False, True])
async def test_empty_date_anchor_terminates_or_starts_at_first_id(oldest: bool) -> None:
    backend, _ = adapter(page([]))
    batch = await backend.read(
        CHAT.id,
        SearchRead(
            "", ReadFilters(date_from=BASE, date_to=BASE, oldest_first=oldest, has_link=True)
        ),
    )
    assert batch.exhausted is not oldest
    assert batch.next_offset == (SearchAnchor(1) if oldest else 0)


async def test_search_date_and_link_filters_preserve_advancing_empty_pages() -> None:
    backend, _ = adapter(page([row(20), row(19), row(18)]))
    batch = await backend.read(
        CHAT.id,
        SearchRead(
            "Post",
            ReadFilters(
                date_from=BASE + timedelta(seconds=18),
                date_to=BASE + timedelta(seconds=20),
                has_link=True,
            ),
        ),
    )
    assert batch.messages == [] and batch.next_offset == 18 and not batch.exhausted


async def test_search_private_sender_is_filtered_locally() -> None:
    backend, client = adapter(
        page([row(20, from_id=types.PeerUser(42)), row(19, from_id=types.PeerUser(43))])
    )
    backend.chat_gateway.cache[CHAT.id] = CHAT.model_copy(update={"type": "private"})
    batch = await backend.read(CHAT.id, SearchRead("Post", sender_id="42"))
    assert [message.sender.name for message in batch.messages] == ["Ada"]
    assert last_request(client).from_id is None


async def test_global_offsets_include_last_raw_filtered_source_and_next_rate() -> None:
    backend, client = adapter(page([row(20), row(19, cid=456)], next_rate=1234))
    batch = await backend.search_global(
        SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
    )
    assert [item.message_ref.message_id for item in batch.messages] == [20]
    assert batch.next_offset == GlobalOffset(1234, "-1000000000456", 19, 9456)
    assert not batch.exhausted and not batch.issues
    request = last_request(client)
    assert isinstance(request, functions.messages.SearchGlobalRequest)
    assert request.folder_id is None and request.community is None
    assert request.broadcasts_only is None and isinstance(request.offset_peer, types.InputPeerEmpty)
    client.get_input_entity.assert_not_called()
    client.get_messages.assert_not_called()
    client.return_value = page([row(18)])
    continued = await backend.search_global(
        SearchRead("Post"), offset=batch.next_offset, limit=30, chat_ids=[CHAT.id]
    )
    request = last_request(client)
    assert request.offset_peer == types.InputPeerChannel(456, 9456)
    assert request.offset_rate == 1234 and request.offset_id == 19
    assert continued.next_offset.rate == int((BASE + timedelta(seconds=18)).timestamp())
    assert client.await_count == 2


async def test_global_keeps_exact_dates_links_and_sender_metadata() -> None:
    backend, client = adapter(
        page(
            [
                row(20),
                row(19, from_id=types.PeerUser(42), entities=[types.MessageEntityUrl(0, 4)]),
                row(18),
            ],
            full=True,
        )
    )
    filters = ReadFilters(
        date_from=BASE + timedelta(seconds=19), date_to=BASE + timedelta(seconds=20), has_link=True
    )
    result = await backend.search_global(
        SearchRead("", filters), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
    )
    assert [item.message_ref.message_id for item in result.messages] == [19]
    assert result.messages[0].sender.name == "Ada" and result.messages[0].has_link
    assert result.exhausted
    request = last_request(client)
    assert isinstance(request.filter, types.InputMessagesFilterUrl)
    assert filters.date_from is not None and filters.date_to is not None
    assert request.min_date == filters.date_from - timedelta(seconds=1)
    assert request.max_date == filters.date_to + timedelta(seconds=1)
    client.assert_awaited_once()


async def test_global_inexact_short_page_keeps_continuation() -> None:
    backend, _ = adapter(page([row(10)], inexact=True))
    result = await backend.search_global(
        SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
    )
    assert not result.exhausted and result.next_offset.message_id == 10
    assert [issue.code for issue in result.issues] == ["SEARCH_INEXACT"]


@pytest.mark.parametrize(
    "response",
    [
        page([row(9), row(10)]),
        page([types.MessageEmpty(id=10, peer_id=None)]),
        types.messages.MessagesNotModified(10),
    ],
)
async def test_global_rejects_unusable_or_unordered_pages(response: Any) -> None:
    backend, _ = adapter(response)
    with pytest.raises(ToolError, match="PAGINATION_INCOMPLETE"):
        await backend.search_global(
            SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
        )


async def test_global_detects_repeated_compound_position() -> None:
    backend, _ = adapter(page([row(10)], next_rate=55))
    with pytest.raises(ToolError, match="PAGINATION_STALLED"):
        await backend.search_global(
            SearchRead("Post"),
            offset=GlobalOffset(55, CHAT.id, 10, 9123),
            limit=30,
            chat_ids=[CHAT.id],
        )


async def test_global_empty_page_is_exhausted() -> None:
    backend, _ = adapter(page([]))
    result = await backend.search_global(
        SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
    )
    assert result.messages == [] and result.exhausted


@pytest.mark.parametrize("source_order", [[10, 9], [9, 10]])
async def test_global_same_second_order_preserves_telegram_order(source_order: list[int]) -> None:
    users: list[Any] = [types.User(id=9, access_hash=90), types.User(id=10, access_hash=100)]
    raws: list[Any] = [
        types.Message(id=1, peer_id=types.PeerUser(uid), date=BASE, message="Post")
        for uid in source_order
    ]
    response = types.messages.MessagesSlice(
        count=2, messages=raws, users=users, chats=[], topics=[]
    )
    backend, _ = adapter(response)
    result = await backend.search_global(
        SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=["9", "10"]
    )
    assert [item.chat.id for item in result.messages] == [str(uid) for uid in source_order]


async def test_sender_search_keeps_native_filter_empty_and_checks_links_locally() -> None:
    backend, client = adapter(
        page(
            [
                row(20, from_id=types.PeerUser(42)),
                row(19, from_id=types.PeerUser(42), entities=[types.MessageEntityUrl(0, 4)]),
            ]
        )
    )
    result = await backend.read(CHAT.id, SearchRead("Post", ReadFilters(has_link=True), "42"))
    assert isinstance(last_request(client).filter, types.InputMessagesFilterEmpty)
    assert [message.message_ref.message_id for message in result.messages] == [19]


async def test_global_preserves_available_rows_when_peer_cannot_be_continued() -> None:
    response = page([row(10)])
    response.chats = []
    backend, client = adapter(response)
    result = await backend.search_global(
        SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
    )
    assert len(result.messages) == 1 and result.exhausted
    assert [issue.code for issue in result.issues] == ["PAGINATION_INCOMPLETE"]
    client.get_input_entity.assert_not_called()


@pytest.mark.parametrize("bad_peer", [object(), types.PeerUser(0)])
async def test_global_invalid_peer_is_structured(bad_peer: Any) -> None:
    raw = row(10)
    raw.peer_id = bad_peer
    backend, _ = adapter(page([raw]))
    with pytest.raises(ToolError, match="PAGINATION_INCOMPLETE"):
        await backend.search_global(
            SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
        )


async def test_global_invalid_entities_are_structured() -> None:
    response = page([row(10)])
    response.users = [object()]
    backend, _ = adapter(response)
    with pytest.raises(ToolError, match="PAGINATION_INCOMPLETE"):
        await backend.search_global(
            SearchRead("Post"), offset=GlobalOffset(), limit=30, chat_ids=[CHAT.id]
        )


@pytest.mark.parametrize("oldest", [False, True])
async def test_search_respects_both_orders_across_raw_pages(oldest: bool) -> None:
    backend, client = adapter(page([row(12), row(11)] if oldest else [row(14), row(13)]))
    request = SearchRead(
        "Post", ReadFilters(date_to=BASE + timedelta(seconds=14), oldest_first=oldest)
    )
    first = await backend.read(CHAT.id, request, offset=10 if oldest else 0, limit=2)
    client.return_value = page([row(14), row(13)] if oldest else [row(12), row(11)])
    second = await backend.read(CHAT.id, request, offset=first.next_offset, limit=2)
    assert [m.message_ref.message_id for m in first.messages + second.messages] == (
        [11, 12, 13] if oldest else [13, 12, 11]
    )
    assert second.exhausted is oldest
    assert client.await_count == 2
