from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from telegram_mcp.config import Settings
from telegram_mcp.models import Chat, MessageRef, ToolError
from telegram_mcp.telegram import (
    TelegramBackend,
    chat_from_entity,
    folder_from_tl,
    normalize_message,
)
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
    assert chat_from_entity(entity, dialog, default_muted=True).is_muted
    dialog.dialog.notify_settings = types.PeerNotifySettings(mute_until=BASE)
    mapped = chat_from_entity(entity, dialog, default_muted=True)
    assert not mapped.is_muted and mapped.unread_mentions_count == 1


async def test_link_resolution_and_rejection_of_external_links() -> None:
    backend = TelegramBackend(Settings())
    backend.chats["-1000000000123"] = Chat(id="-1000000000123", title="News", type="channel")
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
    backend = TelegramBackend(Settings())
    backend.chats["1"] = Chat(id="1", title="Friend", type="private")
    raw = [
        types.Message(
            id=i, peer_id=types.PeerUser(1), date=BASE + timedelta(minutes=i), message="budget"
        )
        for i in (4, 3, 2, 1)
    ]
    client = SimpleNamespace(get_messages=AsyncMock(return_value=raw))
    backend.client = client
    result = await backend.fetch(
        "1",
        limit=4,
        query="budget",
        date_from=BASE + timedelta(minutes=2),
        date_to=BASE + timedelta(minutes=4),
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
        await backend.call(fail())
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.retry_after == 17


async def test_comment_links_resolve_linked_group_before_fetching_a_message() -> None:
    backend = TelegramBackend(Settings())
    backend.chats["-1000000000123"] = Chat(id="-1000000000123", title="News", type="channel")
    backend.discussion_chat = AsyncMock(return_value="-1000000000456")
    ref = await backend.resolve_message("https://t.me/c/123/10?comment=27")
    assert ref == MessageRef(chat_id="-1000000000456", message_id=27)


async def test_discussion_root_uses_actual_group_message_id() -> None:
    backend = TelegramBackend(Settings())
    group_root = types.Message(id=87, peer_id=types.PeerChannel(456), date=BASE, message="post")
    response = SimpleNamespace(messages=[group_root])
    client = AsyncMock(return_value=response)
    client.get_input_entity = AsyncMock(return_value=types.InputPeerChannel(123, 1))
    backend.client = client
    root = await backend.thread_root(
        MessageRef(chat_id="-1000000000123", message_id=10), "-1000000000456"
    )
    assert root == MessageRef(chat_id="-1000000000456", message_id=87)


async def test_catalog_is_not_silently_truncated() -> None:
    backend = TelegramBackend(Settings(max_dialogs=1))
    client = AsyncMock(return_value=SimpleNamespace(filters=[]))
    client.get_dialogs = AsyncMock(return_value=[object(), object()])
    backend.client = client
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
    backend = TelegramBackend(Settings())
    client = AsyncMock(return_value=SimpleNamespace(filters=[raw]))
    client.get_dialogs = AsyncMock(return_value=[])
    client.get_me = AsyncMock(return_value=types.InputPeerUser(123, 456))
    backend.client = client
    _, folders = await backend.catalog()
    assert folders[0].included_chat_ids == ["123"]
    assert folders[0].pinned_chat_ids == ["123"]
