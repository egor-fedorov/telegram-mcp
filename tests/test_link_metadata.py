from typing import Any

import pytest
from telethon import types

from telegram_mcp.adapters.telegram.mapping import normalize_message
from telegram_mcp.domain.models import Chat
from tests.fakes import BASE

CHAT = Chat(id="-1000000000123", title="News", type="channel")


def message(**kwargs: Any) -> types.Message:
    return types.Message(
        id=10, peer_id=types.PeerChannel(123), date=BASE, message="🦉 link", **kwargs
    )


def photo() -> types.Photo:
    return types.Photo(id=1, access_hash=2, file_reference=b"secret", date=BASE, sizes=[], dc_id=1)


def document() -> types.Document:
    return types.Document(
        id=1,
        access_hash=2,
        file_reference=b"secret",
        date=BASE,
        mime_type="video/mp4",
        size=999,
        dc_id=1,
        attributes=[types.DocumentAttributeVideo(duration=2, w=20, h=20)],
    )


@pytest.mark.parametrize("preview_media", [{"photo": photo()}, {"document": document()}, {}])
def test_preview_media_is_metadata_without_attachment(preview_media: dict[str, Any]) -> None:
    raw = message(
        media=types.MessageMediaWebPage(
            types.WebPage(
                id=12345678901234567,
                url="https://example.com/page",
                display_url="example.com/page",
                hash=0,
                type="article",
                site_name="Example",
                title="Title",
                description="Description",
                author="Author",
                **preview_media,
            )
        )
    )
    result = normalize_message(raw, CHAT)
    assert result.content_type == "text" and result.attachments == [] and result.has_link
    assert result.web_page is not None
    assert result.web_page.id == "12345678901234567"
    assert result.web_page.status == "available" and result.web_page.author == "Author"
    assert result.web_page.description == "Description"
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    "page,status",
    [
        (types.WebPagePending(id=1, date=BASE, url="https://example.com"), "pending"),
        (types.WebPageEmpty(id=2, url="https://example.com"), "empty"),
        (types.WebPageNotModified(), "not_modified"),
    ],
)
def test_incomplete_webpage_states_are_preserved(page: Any, status: str) -> None:
    result = normalize_message(message(media=types.MessageMediaWebPage(page)), CHAT)
    assert result.has_link and result.web_page is not None
    assert result.web_page.status == status
    assert result.web_page.title is None
    assert result.web_page.pending_until == (BASE if status == "pending" else None)


@pytest.mark.parametrize(
    "entity",
    [types.MessageEntityUrl(3, 4), types.MessageEntityTextUrl(3, 4, "https://example.com")],
)
def test_link_caption_keeps_media_type_and_original_entities(entity: Any) -> None:
    result = normalize_message(
        message(media=types.MessageMediaPhoto(photo=photo()), entities=[entity]), CHAT
    )
    assert result.has_link and result.content_type == "photo"
    assert result.web_page is None and len(result.attachments) == 1
    assert result.entities[0].offset == 3 and result.entities[0].length == 4


def test_link_detection_does_not_guess_from_text_or_email_entities() -> None:
    raw = message(entities=[types.MessageEntityEmail(3, 4)])
    raw.message = "https://example.com"
    result = normalize_message(raw, CHAT)
    assert not result.has_link and result.web_page is None


def test_sender_mapping_uses_response_entities_without_sdk_initialization() -> None:
    raw = message(from_id=types.PeerUser(42))
    result = normalize_message(raw, CHAT, entities={42: types.User(id=42, first_name="Ada")})
    assert result.sender.id == "42" and result.sender.name == "Ada" and result.sender.type == "user"
