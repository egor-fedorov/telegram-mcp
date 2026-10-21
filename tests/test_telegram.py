from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.adapters.telegram.mapping import (
    chat_from_entity,
    folder_from_tl,
    normalize_message,
)
from telegram_mcp.application.requests import HistoryRead, ReadFilters, SearchRead
from telegram_mcp.domain.models import Chat, MessageRef, ToolError
from tests.fakes import BASE


def test_folder_mapping_with_formatted_title_and_shared_folder() -> None:
    raw = types.DialogFilter(
        id=4,
        title=types.TextWithEntities("AI", []),
        pinned_peers=[types.InputPeerChannel(123, 45)],
        include_peers=[types.InputPeerUser(12, 34)],
        exclude_peers=[types.InputPeerChat(67)],
        broadcasts=True,
        exclude_read=True,
    )
    folder = folder_from_tl(raw, 2)
    assert folder.title == "AI" and folder.position == 2
    assert folder.pinned_chat_ids == ["-1000000000123"]
    assert folder.excluded_chat_ids == ["-67"]
    assert folder.include_channels and folder.exclude_read
    shared = types.DialogFilterChatlist(
        id=5,
        title=types.TextWithEntities("Shared", []),
        pinned_peers=[],
        include_peers=[types.InputPeerUser(12, 34)],
    )
    assert folder_from_tl(shared, 3).included_chat_ids == ["12"]


def test_normalization_preserves_source_links_and_reply_peer() -> None:
    chat = Chat(id="-1000000000123", title="News", type="channel", username="example")
    raw = types.Message(
        id=10,
        peer_id=types.PeerChannel(123),
        date=BASE,
        message="Original post",
        from_id=types.PeerChannel(123),
        reply_to=types.MessageReplyHeader(
            reply_to_msg_id=8, reply_to_peer_id=types.PeerChannel(456)
        ),
    )
    result = normalize_message(raw, chat)
    assert result.text == "Original post"
    assert result.url == "https://t.me/example/10"
    assert result.reply_to == MessageRef(chat_id="-1000000000456", message_id=8)
    private_channel = chat.model_copy(update={"username": None})
    assert normalize_message(raw, private_channel).url == "https://t.me/c/123/10"
    basic_group = Chat(id="-100123", title="Basic", type="group")
    assert normalize_message(raw, basic_group).url is None


def test_inherited_and_explicit_mute_settings() -> None:
    entity = types.User(id=1, first_name="First", contact=True)
    dialog = SimpleNamespace(
        archived=False,
        unread_count=0,
        unread_mentions_count=1,
        dialog=SimpleNamespace(notify_settings=types.PeerNotifySettings(), unread_mark=True),
    )
    assert chat_from_entity(
        entity, dialog, default_muted=True, now=BASE + timedelta(days=1)
    ).is_muted
    dialog.dialog.notify_settings = types.PeerNotifySettings(mute_until=BASE)
    mapped = chat_from_entity(entity, dialog, default_muted=True, now=BASE + timedelta(days=1))
    assert not mapped.is_muted and mapped.unread_mentions_count == 1


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
    client = SimpleNamespace(get_messages=AsyncMock(return_value=raw))
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
    assert client.get_messages.await_args.kwargs["search"] == "budget"
    assert client.get_messages.await_args.kwargs["offset_date"] > BASE + timedelta(minutes=4)


async def test_flood_wait_is_structured_and_does_not_sleep() -> None:
    backend = TelegramBackend(Settings())
    request = functions.messages.GetDialogFiltersRequest()

    async def fail() -> None:
        raise errors.FloodWaitError(request, capture=17)

    with pytest.raises(ToolError) as caught:
        await backend.runtime.call(fail())
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.retry_after == 17


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
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(123, 1))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    root = await backend.thread_root(
        MessageRef(chat_id="-1000000000123", message_id=10), "-1000000000456"
    )
    assert root == MessageRef(chat_id="-1000000000456", message_id=87)


async def test_catalog_is_not_silently_truncated() -> None:
    settings = Settings(max_dialogs=1)
    chat_cache = {}
    client = AsyncMock(return_value=SimpleNamespace(filters=[]))
    client.get_dialogs = AsyncMock(return_value=[object(), object()])
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    with pytest.raises(ToolError, match="CATALOG_LIMIT"):
        await backend.catalog()


async def test_saved_messages_peer_in_folder_uses_account_id() -> None:
    raw = types.DialogFilter(
        id=4,
        title=types.TextWithEntities("Personal", []),
        pinned_peers=[types.InputPeerSelf()],
        include_peers=[types.InputPeerSelf()],
        exclude_peers=[],
    )
    settings = Settings()
    chat_cache = {}
    client = AsyncMock(return_value=SimpleNamespace(filters=[raw]))
    client.get_dialogs = AsyncMock(return_value=[])
    client.get_me = AsyncMock(return_value=types.InputPeerUser(123, 456))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    _, folders = await backend.catalog()
    assert folders[0].included_chat_ids == ["123"]
    assert folders[0].pinned_chat_ids == ["123"]


def channel(
    *,
    broadcast: bool = False,
    megagroup: bool = False,
    username: str | None = None,
    usernames: list[types.Username] | None = None,
) -> types.Channel:
    return types.Channel(
        id=123,
        title="News",
        photo=types.ChatPhotoEmpty(),
        date=BASE,
        broadcast=broadcast,
        megagroup=megagroup,
        username=username,
        usernames=usernames,
    )


@pytest.mark.parametrize(
    "reference",
    [
        "-1000000000123",
        "@example",
        "https://t.me/example",
        "https://t.me/s/example",
        "t.me/s/example",
    ],
)
async def test_chat_references_resolve_to_canonical_id(reference: str) -> None:
    settings = Settings()
    chat_cache = {}
    client = SimpleNamespace(
        get_entity=AsyncMock(return_value=channel(broadcast=True, username="example"))
    )
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    resolved = await backend.resolve_chat(reference)
    assert resolved.id == "-1000000000123" and resolved.username == "example"
    await backend.resolve_chat(resolved.id)
    assert client.get_entity.await_count == 1
    await backend.resolve_chat(resolved.id, refresh=True)
    assert client.get_entity.await_count == 2


async def test_unknown_numeric_chat_suggests_username_without_leaking_error() -> None:
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings,
        runtime=TelegramRuntime(
            settings, client=SimpleNamespace(get_entity=AsyncMock(side_effect=ValueError("secret")))
        ),
        chat_cache=chat_cache,
    )
    with pytest.raises(ToolError, match="username or t.me link") as caught:
        await backend.resolve_chat("-1000000000123")
    assert caught.value.code == "INVALID_REFERENCE"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "entity",
    [
        channel(broadcast=True, username="example"),
        channel(broadcast=True, usernames=[types.Username("example", active=True)]),
    ],
)
async def test_public_channel_checks_fresh_entity_and_active_usernames(
    entity: types.Channel,
) -> None:
    settings = Settings()
    chat_cache = {}
    chat_cache["-1000000000123"] = Chat(id="-1000000000123", title="Stale", type="group")
    client = SimpleNamespace(get_entity=AsyncMock(return_value=entity))
    backend = TelegramBackend(
        settings, runtime=TelegramRuntime(settings, client=client), chat_cache=chat_cache
    )
    resolved = await backend.public_channel("-1000000000123")
    assert resolved.type == "channel" and resolved.username == "example"
    assert resolved.title == "News"
    client.get_entity.assert_awaited_once_with(-1000000000123)
    client.get_entity.return_value = channel(broadcast=True)
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await backend.public_channel(resolved.id)
    assert client.get_entity.await_count == 2


@pytest.mark.parametrize(
    "entity",
    [
        types.User(id=123, username="example"),
        types.ChannelForbidden(id=123, access_hash=1, title="Forbidden", broadcast=True),
        channel(broadcast=False, megagroup=True, username="example"),
        channel(broadcast=True, megagroup=True, username="example"),
        channel(broadcast=True, usernames=[types.Username("example", active=False)]),
        channel(broadcast=True),
    ],
)
async def test_public_exception_rejects_private_and_non_broadcast_entities(entity: object) -> None:
    settings = Settings()
    chat_cache = {}
    backend = TelegramBackend(
        settings,
        runtime=TelegramRuntime(
            settings, client=SimpleNamespace(get_entity=AsyncMock(return_value=entity))
        ),
        chat_cache=chat_cache,
    )
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await backend.public_channel("-1000000000123")


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
        get_messages=AsyncMock(
            return_value=[None, message, types.MessageEmpty(id=12, peer_id=types.PeerChannel(123))]
        )
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


def test_forward_sources_remain_separate_for_saved_and_hidden_senders() -> None:
    chat = Chat(id="1", title="Saved Messages", type="saved")
    raw = types.Message(
        id=10,
        peer_id=types.PeerUser(1),
        date=BASE + timedelta(days=2),
        message="forward",
        fwd_from=types.MessageFwdHeader(
            date=BASE,
            from_id=types.PeerChannel(123),
            channel_post=7,
            post_author="Original author",
            saved_from_peer=types.PeerChannel(456),
            saved_from_msg_id=8,
            saved_from_id=types.PeerUser(42),
            saved_from_name="Saved author",
            saved_date=BASE + timedelta(days=1),
        ),
    )
    origin = normalize_message(raw, chat).forward_origin
    assert origin is not None
    assert origin.sender_id == "-1000000000123" and origin.post_author == "Original author"
    assert origin.message_ref == MessageRef(chat_id="-1000000000123", message_id=7)
    assert origin.saved_from_ref == MessageRef(chat_id="-1000000000456", message_id=8)
    assert origin.saved_from_sender_id == "42" and origin.saved_from_name == "Saved author"
    assert origin.sent_at == BASE and origin.saved_at == BASE + timedelta(days=1)
    raw.fwd_from = types.MessageFwdHeader(date=BASE, from_name="Hidden author")
    hidden = normalize_message(raw, chat).forward_origin
    assert hidden is not None and hidden.name == "Hidden author"
    assert hidden.sender_id is None and hidden.message_ref is None and hidden.saved_from_ref is None


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
    client = SimpleNamespace(get_messages=AsyncMock(side_effect=[raw[:2], raw[2:]]))
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
                get_messages=AsyncMock(
                    return_value=[
                        types.MessageEmpty(id=i, peer_id=types.PeerUser(1)) for i in (20, 19)
                    ]
                )
            ),
        ),
        chat_cache=chat_cache,
    )
    page = await backend.read("1", HistoryRead(filters=ReadFilters()), limit=2)
    assert page.messages == [] and page.next_offset == 19 and not page.exhausted


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (errors.RpcCallFailError(None), "TELEGRAM_UNAVAILABLE", True),
        (errors.TimedOutError(None, "timeout"), "TELEGRAM_UNAVAILABLE", True),
        (ConnectionError("secret"), "CONNECTION_ERROR", True),
        (errors.SessionExpiredError(None), "AUTH_REQUIRED", False),
        (errors.ChannelPrivateError(None), "TELEGRAM_ERROR", False),
    ],
)
async def test_error_retryability_is_explicit(error: Exception, code: str, retryable: bool) -> None:
    backend = TelegramBackend(Settings())
    with pytest.raises(ToolError) as caught:
        await backend.runtime.call(AsyncMock(side_effect=error)())
    assert caught.value.code == code and caught.value.retryable is retryable
    issue = caught.value.issue(input_index=3)
    assert issue.input_index == 3 and issue.message_ref is None
