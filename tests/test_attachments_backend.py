"""Selected downloads use public APIs, current protection metadata, and bounded recovery."""

import asyncio
from collections.abc import Buffer
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import TelegramClient, errors, types
from telethon.sessions import MemorySession

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.application.attachment_ports import MAX_ATTACHMENT_BYTES, AttachmentKind
from telegram_mcp.domain.models import MessageRef, ToolError
from tests.fakes import BASE

REF = MessageRef(chat_id="-1000000000123", message_id=7)


def document(**values: Any) -> types.Document:
    fields: dict[str, Any] = {
        "id": 10,
        "access_hash": 20,
        "file_reference": b"private-reference",
        "date": BASE,
        "mime_type": "application/pdf",
        "size": 5,
        "dc_id": 2,
        "attributes": [types.DocumentAttributeFilename("original 🦉.pdf")],
        **values,
    }
    return types.Document(**fields)


def photo(**values: Any) -> types.Photo:
    fields: dict[str, Any] = {
        "id": 30,
        "access_hash": 40,
        "file_reference": b"private-reference",
        "date": BASE,
        "dc_id": 2,
        "sizes": [types.PhotoSize("x", 1280, 720, 5)],
        **values,
    }
    return types.Photo(**fields)


def message(**values: Any) -> types.Message:
    fields: dict[str, Any] = {
        "id": REF.message_id,
        "peer_id": types.PeerChannel(123),
        "date": BASE,
        "message": "Original caption",
        "media": types.MessageMediaDocument(document=document()),
        **values,
    }
    return types.Message(**fields)


def adapter(raw: Any = None) -> tuple[TelegramBackend, AsyncMock]:
    client = AsyncMock()
    client.is_connected = Mock(return_value=True)
    client.get_entity.return_value = types.Channel(
        id=123, title="Selected channel", photo=types.ChatPhotoEmpty(), date=BASE, broadcast=True
    )
    client.get_messages.return_value = [raw if raw is not None else message()]

    async def download(media: Any, *, file: BytesIO, thumb: Any) -> BytesIO:
        file.write(b"hello")
        return file

    client.download_media.side_effect = download
    settings = Settings()
    backend = TelegramBackend(settings, runtime=TelegramRuntime(settings, client=client))
    return backend, client


@pytest.mark.parametrize(
    "attributes",
    [
        [],
        [types.DocumentAttributeAudio(duration=3, voice=True)],
        [types.DocumentAttributeAudio(duration=3, title="Music")],
        [types.DocumentAttributeVideo(duration=3, w=640, h=480)],
        [types.DocumentAttributeVideo(duration=3, w=640, h=480, round_message=True)],
    ],
)
async def test_document_media_types_use_original_document_without_enrichment(
    attributes: list[Any],
) -> None:
    media = document(attributes=[types.DocumentAttributeFilename("original 🦉.pdf"), *attributes])
    raw = message(
        media=types.MessageMediaDocument(document=media),
        fwd_from=types.MessageFwdHeader(date=BASE, from_id=types.PeerChannel(999)),
    )
    backend, client = adapter(raw)
    source = await backend.prepare_attachment(REF, "document")
    assert source.attachment_id == f"{REF.key}:document"
    assert source.message_ref == REF and source.identity == "document:10"
    assert source.name == "original 🦉.pdf" and source.mime_type == "application/pdf"
    assert source.size == 5
    writer = BytesIO()
    await backend.download_attachment(source, writer)
    assert writer.getvalue() == b"hello"
    client.get_entity.assert_awaited_once_with(int(REF.chat_id))
    client.get_messages.assert_awaited_once_with(client.get_entity.return_value, ids=[7])
    client.download_media.assert_awaited_once_with(media, file=writer, thumb=None)
    client.assert_not_awaited()


async def test_largest_static_photo_is_explicit_even_when_video_size_is_larger() -> None:
    largest = types.PhotoSizeProgressive("y", 2560, 1440, [2, 5])
    media = photo(
        sizes=[
            largest,
            types.PhotoSize("x", 1280, 720, 4),
            types.PhotoStrippedSize("i", b"preview"),
        ],
        video_sizes=[types.VideoSize("u", 5120, 2880, 100)],
    )
    backend, client = adapter(message(media=types.MessageMediaPhoto(photo=media)))
    source = await backend.prepare_attachment(REF, "photo")
    assert source.identity == "photo:30:y" and source.size == 5
    assert source.name == "photo-7.jpg" and source.mime_type == "image/jpeg"
    writer = BytesIO()
    await backend.download_attachment(source, writer)
    client.download_media.assert_awaited_once_with(media, file=writer, thumb=largest.type)


async def test_real_public_telethon_download_accepts_progressive_still_photo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    largest = types.PhotoSizeProgressive("y", 2560, 1440, [2, 5])
    media = photo(sizes=[largest], video_sizes=[types.VideoSize("u", 5120, 2880, 100)])
    backend, fake = adapter(message(media=types.MessageMediaPhoto(photo=media)))
    client = TelegramClient(MemorySession(), 123, "synthetic")
    monkeypatch.setattr(client, "is_connected", lambda: True)
    monkeypatch.setattr(client, "get_entity", fake.get_entity)
    monkeypatch.setattr(client, "get_messages", fake.get_messages)

    async def download_file(location: Any, file: BytesIO, **kwargs: Any) -> None:
        assert isinstance(location, types.InputPhotoFileLocation)
        assert location.id == media.id and location.thumb_size == "y"
        assert kwargs["file_size"] == 5
        file.write(b"photo")

    transfer = AsyncMock(side_effect=download_file)
    monkeypatch.setattr(client, "download_file", transfer)
    backend.runtime.client = client
    source = await backend.prepare_attachment(REF, "photo")
    writer = BytesIO()
    await backend.download_attachment(source, writer)
    assert writer.getvalue() == b"photo"
    transfer.assert_awaited_once()


async def test_photo_never_falls_back_to_video_or_embedded_preview() -> None:
    backend, client = adapter(
        message(
            media=types.MessageMediaPhoto(
                photo=photo(
                    sizes=[types.PhotoStrippedSize("i", b"preview")],
                    video_sizes=[types.VideoSize("u", 5120, 2880, 100)],
                )
            )
        )
    )
    with pytest.raises(ToolError, match="ATTACHMENT_UNAVAILABLE"):
        await backend.prepare_attachment(REF, "photo")
    client.download_media.assert_not_called()


@pytest.mark.parametrize("protection", ["entity", "message", "media_ttl"])
async def test_current_protection_blocks_downloads(protection: str) -> None:
    raw = message()
    backend, client = adapter(raw)
    if protection == "entity":
        client.get_entity.return_value.noforwards = True
    elif protection == "message":
        raw.noforwards = True
    else:
        assert isinstance(raw.media, types.MessageMediaDocument)
        raw.media.ttl_seconds = 60
    with pytest.raises(ToolError, match="ATTACHMENT_PROTECTED"):
        await backend.prepare_attachment(REF, "document")
    client.download_media.assert_not_called()
    if protection == "entity":
        client.get_messages.assert_not_called()


async def test_message_auto_delete_and_spoilers_do_not_imply_media_saving_protection() -> None:
    backend, _ = adapter(
        message(
            ttl_period=60,
            media=types.MessageMediaDocument(document=document(), ttl_seconds=0, spoiler=True),
        )
    )
    assert (await backend.prepare_attachment(REF, "document")).size == 5


@pytest.mark.parametrize(
    ("media", "kind"),
    [
        (None, "document"),
        (types.MessageMediaDocument(document=types.DocumentEmpty(1)), "document"),
        (types.MessageMediaPhoto(photo=types.PhotoEmpty(1)), "photo"),
        (types.MessageMediaPhoto(photo=photo()), "document"),
        (types.MessageMediaDocument(document=document()), "photo"),
        (types.MessageMediaWebPage(types.WebPageEmpty(1)), "photo"),
        (types.MessageMediaPaidMedia(stars_amount=1, extended_media=[]), "document"),
        (types.MessageMediaStory(peer=types.PeerChannel(123), id=1), "photo"),
    ],
)
async def test_unsupported_media_cannot_download_through_convenience_api(
    media: Any, kind: AttachmentKind
) -> None:
    backend, client = adapter(message(media=media))
    with pytest.raises(ToolError, match="ATTACHMENT_UNAVAILABLE"):
        await backend.prepare_attachment(REF, kind)
    client.download_media.assert_not_called()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [None],
        [types.MessageEmpty(id=7, peer_id=None)],
        [message(id=8)],
        [message(peer_id=types.PeerChannel(999))],
        [message(peer_id=None)],
        [message(), message()],
    ],
)
async def test_absent_or_mismatched_messages_never_provide_attachment(rows: Any) -> None:
    backend, client = adapter()
    client.get_messages.return_value = rows
    with pytest.raises(ToolError, match="ATTACHMENT_UNAVAILABLE"):
        await backend.prepare_attachment(REF, "document")
    client.download_media.assert_not_called()


async def test_fresh_entity_must_match_canonical_source() -> None:
    backend, client = adapter()
    client.get_entity.return_value.id = 456
    with pytest.raises(ToolError, match="ATTACHMENT_UNAVAILABLE"):
        await backend.prepare_attachment(REF, "document")
    client.get_messages.assert_not_called()


@pytest.mark.parametrize("kind", ["document", "photo"])
@pytest.mark.parametrize("size", [MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_BYTES + 1])
async def test_declared_size_limit_is_checked_before_downloading(
    kind: AttachmentKind, size: int
) -> None:
    media = (
        types.MessageMediaDocument(document=document(size=size))
        if kind == "document"
        else types.MessageMediaPhoto(photo=photo(sizes=[types.PhotoSize("x", 1280, 720, size)]))
    )
    backend, client = adapter(message(media=media))
    if size == MAX_ATTACHMENT_BYTES:
        assert (await backend.prepare_attachment(REF, kind)).size == size
    else:
        with pytest.raises(ToolError, match="ATTACHMENT_TOO_LARGE"):
            await backend.prepare_attachment(REF, kind)
    client.download_media.assert_not_called()


@pytest.mark.parametrize("declared", [None, -1, True, "unknown", 1])
async def test_missing_or_inaccurate_size_still_uses_bounded_writer(declared: Any) -> None:
    backend, client = adapter(
        message(media=types.MessageMediaDocument(document=document(size=declared)))
    )
    source = await backend.prepare_attachment(REF, "document")
    assert source.size == (1 if type(declared) is int and declared == 1 else None)

    class BoundedWriter(BytesIO):
        def write(self, data: Buffer, /) -> int:
            raise ToolError("ATTACHMENT_TOO_LARGE", "Actual download size exceeds the limit.")

    with pytest.raises(ToolError, match="ATTACHMENT_TOO_LARGE"):
        await backend.download_attachment(source, BoundedWriter())
    client.download_media.assert_awaited_once()


@pytest.mark.parametrize(
    "error_type",
    [
        errors.FileReferenceExpiredError,
        errors.FileReferenceInvalidError,
        errors.FileReferenceEmptyError,
        errors.FilerefUpgradeNeededError,
    ],
)
async def test_reference_refresh_rechecks_source_and_restarts_writer_once(error_type: Any) -> None:
    backend, client = adapter()
    source = await backend.prepare_attachment(REF, "document")
    fresh = document(file_reference=b"refreshed", size=3)
    client.get_messages.return_value = [message(media=types.MessageMediaDocument(document=fresh))]
    count = 0

    async def download(media: Any, *, file: BytesIO, thumb: Any) -> BytesIO:
        nonlocal count
        count += 1
        if count == 1:
            file.write(b"old partial bytes")
            raise error_type(None)
        assert file.tell() == 0 and file.getvalue() == b""
        assert media is fresh
        file.write(b"new")
        return file

    client.download_media.side_effect = download
    writer = BytesIO()
    await backend.download_attachment(source, writer)
    assert writer.getvalue() == b"new"
    assert client.get_entity.await_count == client.get_messages.await_count == 2
    assert client.download_media.await_count == 2


@pytest.mark.parametrize("change", ["identity", "protection", "size", "missing"])
async def test_reference_refresh_cannot_bypass_current_source_state(change: str) -> None:
    backend, client = adapter()
    source = await backend.prepare_attachment(REF, "document")
    fresh = message()
    assert isinstance(fresh.media, types.MessageMediaDocument)
    assert isinstance(fresh.media.document, types.Document)
    expected = "ATTACHMENT_UNAVAILABLE"
    if change == "identity":
        fresh.media.document.id = 99
    elif change == "protection":
        fresh.noforwards = True
        expected = "ATTACHMENT_PROTECTED"
    elif change == "size":
        fresh.media.document.size = MAX_ATTACHMENT_BYTES + 1
        expected = "ATTACHMENT_TOO_LARGE"
    client.get_messages.return_value = [None if change == "missing" else fresh]
    client.download_media.side_effect = errors.FileReferenceExpiredError(None)
    with pytest.raises(ToolError, match=expected):
        await backend.download_attachment(source, BytesIO())
    client.download_media.assert_awaited_once()
    assert client.get_messages.await_count == 2


async def test_second_reference_failure_stops_without_another_refetch() -> None:
    backend, client = adapter()
    source = await backend.prepare_attachment(REF, "document")
    client.download_media.side_effect = errors.FileReferenceExpiredError(None)
    with pytest.raises(ToolError, match="ATTACHMENT_UNAVAILABLE"):
        await backend.download_attachment(source, BytesIO())
    assert client.download_media.await_count == client.get_messages.await_count == 2


@pytest.mark.parametrize(
    ("error", "code", "retry_after"),
    [
        (errors.FloodWaitError(None, capture=17), "RATE_LIMITED", 17),
        (ConnectionError("private-value"), "CONNECTION_ERROR", None),
        (errors.ServerError(None, "private-value", 500), "TELEGRAM_UNAVAILABLE", None),
        (errors.ChatForwardsRestrictedError(None), "ATTACHMENT_PROTECTED", None),
    ],
)
async def test_download_transport_failures_are_structured_without_replays(
    error: Exception, code: str, retry_after: int | None
) -> None:
    backend, client = adapter()
    source = await backend.prepare_attachment(REF, "document")
    client.download_media.side_effect = error
    with pytest.raises(ToolError) as caught:
        await backend.download_attachment(source, BytesIO())
    assert caught.value.code == code and caught.value.retry_after == retry_after
    assert "private-value" not in str(caught.value)
    client.download_media.assert_awaited_once()
    client.get_messages.assert_awaited_once()


async def test_empty_download_is_reported_as_unavailable() -> None:
    backend, client = adapter()
    source = await backend.prepare_attachment(REF, "document")
    client.download_media.side_effect = None
    client.download_media.return_value = None
    with pytest.raises(ToolError, match="ATTACHMENT_UNAVAILABLE"):
        await backend.download_attachment(source, BytesIO())


@pytest.mark.parametrize("size", [0, 4, 6])
async def test_successful_transport_with_mismatched_known_size_is_not_complete(size: int) -> None:
    backend, client = adapter(
        message(media=types.MessageMediaDocument(document=document(size=size)))
    )
    source = await backend.prepare_attachment(REF, "document")
    with pytest.raises(ToolError, match="ATTACHMENT_UNAVAILABLE.*unexpected attachment size"):
        await backend.download_attachment(source, BytesIO())
    client.download_media.assert_awaited_once()


async def test_unknown_size_can_complete_without_inventing_a_declared_size() -> None:
    backend, client = adapter(
        message(media=types.MessageMediaDocument(document=document(size=None)))
    )
    source = await backend.prepare_attachment(REF, "document")
    writer = BytesIO()
    await backend.download_attachment(source, writer)
    assert writer.getvalue() == b"hello"
    client.download_media.assert_awaited_once()


async def test_attachment_download_uses_extended_timeout_without_changing_metadata_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_for = AsyncMock(wraps=asyncio.wait_for)
    monkeypatch.setattr("telegram_mcp.adapters.telegram.runtime.asyncio.wait_for", wait_for)
    backend, _ = adapter()
    source = await backend.prepare_attachment(REF, "document")
    await backend.download_attachment(source, BytesIO())
    assert [call.args[1] for call in wait_for.await_args_list] == [20, 60]


async def test_cancelled_download_is_not_retried_or_converted() -> None:
    backend, client = adapter()
    source = await backend.prepare_attachment(REF, "document")
    client.download_media.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await backend.download_attachment(source, BytesIO())
    client.download_media.assert_awaited_once()


async def test_runtime_timeout_override_keeps_structured_connection_error() -> None:
    backend, _ = adapter()

    async def blocked() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await backend.runtime.call(blocked(), timeout_seconds=0.001)
