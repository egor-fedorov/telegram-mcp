import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from telegram_mcp.config import Settings
from telegram_mcp.models import Chat, MessageRef, ToolError
from telegram_mcp.telegram import TelegramBackend
from tests.fakes import BASE

CHAT = Chat(id="-1000000000123", title="News", type="channel", username="example")


def raw_message(mid: int, **kwargs: Any) -> types.Message:
    values: dict[str, Any] = {
        "id": mid,
        "peer_id": types.PeerChannel(123),
        "date": BASE,
        "message": f"Original 🦉 caption {mid}",
        "grouped_id": 987654321,
        **kwargs,
    }
    return types.Message(**values)


def message_page(rows: list[Any], kind: str = "channel") -> Any:
    values: dict[str, Any] = {"messages": rows, "users": [], "chats": [], "topics": []}
    if kind == "plain":
        return types.messages.Messages(**values)
    if kind == "slice":
        return types.messages.MessagesSlice(count=1000, **values)
    return types.messages.ChannelMessages(pts=1, count=1000, **values)


def adapter(response: Any) -> tuple[TelegramBackend, AsyncMock]:
    client = AsyncMock(return_value=response)
    client.get_input_entity.return_value = types.InputPeerChannel(123, 456)
    client._self_id = 999
    client._mb_entity_cache = SimpleNamespace(get=lambda _: None)
    backend = TelegramBackend(Settings())
    backend.chats[CHAT.id] = CHAT
    backend.client = client
    return backend, client


@pytest.mark.parametrize("newer", [False, True])
async def test_album_window_uses_one_raw_request_and_filters_direction(newer: bool) -> None:
    rows = [
        raw_message(mid, grouped_id=None if mid == 51 else 987654321)
        for mid in [52, 49, 50, 48, 51]
    ]
    backend, client = adapter(message_page(rows))
    window = await backend.history_window(CHAT.id, anchor_id=50, limit=7, newer=newer)
    client.assert_awaited_once()
    assert client.await_args is not None
    request = client.await_args.args[0]
    assert isinstance(request, functions.messages.GetHistoryRequest)
    assert request.peer == types.InputPeerChannel(123, 456)
    assert request.offset_id == (51 if newer else 50)
    assert request.add_offset == (-7 if newer else 0)
    assert request.limit == 7 and request.offset_date is None
    assert request.max_id == request.min_id == request.hash == 0
    assert [message.message_ref.message_id for message in window.messages] == (
        [51, 52] if newer else [49, 48]
    )
    assert window.scanned_count == 5 and not window.exhausted and not window.issues
    if newer:
        assert window.messages[0].album_id is None
    client.get_messages.assert_not_called()
    client.download_media.assert_not_called()


@pytest.mark.parametrize("kind", ["plain", "slice", "channel"])
@pytest.mark.parametrize("count", [0, 1, 2])
async def test_album_window_exhaustion_requires_evidence(kind: str, count: int) -> None:
    backend, client = adapter(message_page([raw_message(20 - i) for i in range(count)], kind))
    window = await backend.history_window(CHAT.id, anchor_id=30, limit=2, newer=False)
    assert window.exhausted is (count == 0 or kind == "plain" and count < 2)
    assert window.scanned_count == count
    client.assert_awaited_once()


async def test_album_window_preserves_source_metadata_without_enrichment() -> None:
    source = raw_message(
        49,
        from_id=types.PeerUser(42),
        entities=[types.MessageEntityCustomEmoji(9, 2, 2**62)],
        views=0,
        forwards=3,
        reactions=types.MessageReactions(
            results=[types.ReactionCount(types.ReactionEmoji("👍"), 2)]
        ),
        media=types.MessageMediaPhoto(
            photo=types.Photo(
                id=6, access_hash=7, file_reference=b"private", date=BASE, sizes=[], dc_id=2
            )
        ),
        fwd_from=types.MessageFwdHeader(date=BASE, from_name="Hidden source"),
    )
    page = message_page([source])
    page.users = [types.User(id=42, first_name="Original sender")]
    backend, client = adapter(page)
    window = await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=False)
    result = window.messages[0]
    assert result.text == "Original 🦉 caption 49" and result.text_total_length == len(result.text)
    assert result.entities[0].document_id == str(2**62)
    assert result.sent_at == BASE and result.url == "https://t.me/example/49"
    assert result.sender.id == "42" and result.sender.name == "Original sender"
    assert result.album_id == "987654321"
    assert result.view_count == 0 and result.forward_count == 3
    assert result.reactions is not None and result.reactions.results[0].count == 2
    assert result.forward_origin is not None and result.forward_origin.name == "Hidden source"
    assert result.attachments[0].type == "photo" and result.content_type == "photo"
    assert "private" not in result.model_dump_json()
    assert len(client.mock_calls) == 2


@pytest.mark.parametrize("with_peer", [False, True])
async def test_album_window_reports_known_empty_messages_but_not_numeric_gaps(
    with_peer: bool,
) -> None:
    empty = types.MessageEmpty(id=47, peer_id=types.PeerChannel(123) if with_peer else None)
    backend, _ = adapter(message_page([raw_message(49), empty, raw_message(45)], "plain"))
    window = await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=False)
    assert [item.message_ref.message_id for item in window.messages] == [49, 45]
    assert window.scanned_count == 3 and not window.exhausted
    assert len(window.issues) == 1
    issue = window.issues[0]
    assert issue.code == "MESSAGE_UNAVAILABLE"
    assert issue.message_ref == MessageRef(chat_id=CHAT.id, message_id=47)
    assert "album membership is unknown" in issue.message
    assert "deleted" not in issue.message.lower()


async def test_album_window_deduplicates_and_prefers_available_message_over_placeholder() -> None:
    backend, _ = adapter(
        message_page(
            [
                types.MessageEmpty(id=47, peer_id=None),
                raw_message(47),
                raw_message(49),
                raw_message(49),
            ]
        )
    )
    window = await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=False)
    assert [item.message_ref.message_id for item in window.messages] == [49, 47]
    assert window.scanned_count == 4 and not window.issues


async def test_album_window_never_exposes_foreign_chat_rows_or_identifiers() -> None:
    foreign_peer = types.PeerChannel(888888)
    backend, _ = adapter(
        message_page(
            [
                raw_message(777777, peer_id=foreign_peer, message="foreign-secret"),
                types.MessageEmpty(id=666666, peer_id=foreign_peer),
                raw_message(49),
            ],
            "plain",
        )
    )
    window = await backend.history_window(CHAT.id, anchor_id=900000, limit=10, newer=False)
    assert [item.message_ref.message_id for item in window.messages] == [49]
    assert [item.code for item in window.issues] == ["ALBUM_SCAN_INCOMPLETE"]
    assert not window.exhausted
    serialized = json.dumps(
        [item.model_dump(mode="json") for item in [*window.messages, *window.issues]]
    )
    assert all(
        secret not in serialized for secret in ["foreign-secret", "888888", "777777", "666666"]
    )


@pytest.mark.parametrize(
    "row",
    [
        raw_message(0),
        raw_message(True),
        raw_message(49, date=None),
        raw_message(49, peer_id=None),
        raw_message(49, peer_id=object()),
        SimpleNamespace(id=49, peer_id=types.PeerChannel(123), date=BASE),
    ],
)
async def test_album_window_rejects_incomplete_rows_without_claiming_exhaustion(row: Any) -> None:
    backend, _ = adapter(message_page([row], "plain"))
    window = await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=False)
    assert not window.messages and not window.exhausted
    assert window.scanned_count == 1
    assert len(window.issues) == 1 and window.issues[0].code == "ALBUM_SCAN_INCOMPLETE"


async def test_album_window_preserves_service_messages_and_limits_raw_slots() -> None:
    service = types.MessageService(
        id=49, peer_id=types.PeerChannel(123), date=BASE, action=types.MessageActionPinMessage()
    )
    backend, _ = adapter(message_page([service, raw_message(48), raw_message(47)], "plain"))
    window = await backend.history_window(CHAT.id, anchor_id=50, limit=2, newer=False)
    assert [item.message_ref.message_id for item in window.messages] == [49, 48]
    assert window.messages[0].content_type == "other"
    assert window.scanned_count == 2 and not window.exhausted
    assert window.issues[0].code == "ALBUM_SCAN_INCOMPLETE"


@pytest.mark.parametrize(
    "result", [None, types.messages.MessagesNotModified(count=10), SimpleNamespace(messages=[])]
)
async def test_album_window_rejects_unsupported_response_variants(result: Any) -> None:
    backend, client = adapter(result)
    with pytest.raises(ToolError, match="ALBUM_SCAN_INCOMPLETE"):
        await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=False)
    client.assert_awaited_once()


@pytest.mark.parametrize("field", ["messages", "users", "chats"])
async def test_album_window_rejects_malformed_response_vectors(field: str) -> None:
    result = message_page([])
    setattr(result, field, None)
    backend, _ = adapter(result)
    with pytest.raises(ToolError, match="ALBUM_SCAN_INCOMPLETE"):
        await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=False)


@pytest.mark.parametrize(("anchor_id", "newer"), [(1, False), (2**31 - 1, True)])
async def test_album_window_handles_integer_boundaries_without_a_request(
    anchor_id: int, newer: bool
) -> None:
    backend, client = adapter(None)
    window = await backend.history_window(CHAT.id, anchor_id=anchor_id, limit=100, newer=newer)
    assert not window.messages and window.scanned_count == 0 and window.exhausted
    assert not window.issues and not client.mock_calls


@pytest.mark.parametrize("limit", [0, 101, True, "10", 10.5, None])
async def test_album_window_rejects_invalid_limit_before_requests(limit: Any) -> None:
    backend, client = adapter(None)
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await backend.history_window(CHAT.id, anchor_id=50, limit=limit, newer=False)
    assert not client.mock_calls


@pytest.mark.parametrize("anchor_id", [0, -1, 2**31, True, "50", 50.5])
async def test_album_window_rejects_invalid_anchor_before_requests(anchor_id: Any) -> None:
    backend, client = adapter(None)
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await backend.history_window(CHAT.id, anchor_id=anchor_id, limit=10, newer=False)
    assert not client.mock_calls


@pytest.mark.parametrize("newer", [0, 1, None])
async def test_album_window_rejects_invalid_direction_before_requests(newer: Any) -> None:
    backend, client = adapter(None)
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=newer)
    assert not client.mock_calls


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (errors.FloodWaitError(request=None, capture=17), "RATE_LIMITED"),
        (ConnectionError(), "CONNECTION_ERROR"),
        (errors.ServerError(request=None, message="INTERNAL"), "TELEGRAM_UNAVAILABLE"),
    ],
)
async def test_album_window_preserves_temporary_failures_without_retrying(
    failure: Exception, code: str
) -> None:
    backend, client = adapter(None)
    client.side_effect = failure
    with pytest.raises(ToolError) as caught:
        await backend.history_window(CHAT.id, anchor_id=50, limit=10, newer=False)
    assert caught.value.code == code
    if code == "RATE_LIMITED":
        assert caught.value.retry_after == 17
    client.assert_awaited_once()
