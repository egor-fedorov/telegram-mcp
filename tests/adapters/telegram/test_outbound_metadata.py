"""Current Telethon TL types preserve button rows, raw direction and membership."""

import pytest
from telethon import types

from telegram_mcp.adapters.telegram.buttons import normalize_markup
from telegram_mcp.adapters.telegram.mapping import chat_from_entity, normalize_message
from telegram_mcp.domain.models import Chat
from telegram_mcp.domain.text import clip
from tests.support.fakes import BASE


def keyboard(data=b"private-payload"):
    return types.ReplyInlineMarkup(
        rows=[
            types.KeyboardInlineButtonRow(
                buttons=[
                    types.KeyboardInlineButton(
                        text="Apply", type=types.InlineButtonTypeUrl(url="https://example.test/job")
                    ),
                    types.KeyboardInlineButton(
                        text="Details", type=types.InlineButtonTypeCallback(data=data)
                    ),
                ]
            )
        ]
    )


def test_button_payload_changes_revision_but_never_leaks_payload():
    chat = Chat(id="2", title="Bot", type="bot")
    raw = types.Message(
        id=4,
        peer_id=types.PeerUser(2),
        date=BASE,
        message="A long body",
        out=True,
        reply_markup=keyboard(),
    )
    first = normalize_message(raw, chat)
    assert first.reply_markup is not None
    assert (
        first.outgoing is True and first.reply_markup.rows[0][0].url == "https://example.test/job"
    )
    assert first.reply_markup.rows[0][1].type == "callback"
    assert "private-payload" not in first.model_dump_json()
    assert clip(first, 2).revision == first.revision
    raw.views = 100
    assert normalize_message(raw, chat).revision == first.revision
    raw.reply_markup = keyboard(b"other-payload")
    assert normalize_message(raw, chat).revision != first.revision
    raw.out = False
    raw.from_id = types.PeerUser(101)
    assert normalize_message(raw, chat).outgoing is False


@pytest.mark.parametrize(
    "left,minimal,expected",
    [
        (False, False, "joined"),
        (True, False, "not_joined"),
        (True, True, "unknown"),
        (False, True, "unknown"),
    ],
)
def test_channel_membership_uses_fresh_full_metadata(left, minimal, expected):
    entity = types.Channel(
        id=123,
        title="News",
        photo=types.ChatPhotoEmpty(),
        date=BASE,
        broadcast=True,
        left=left,
        min=minimal,
        username="news",
    )
    assert chat_from_entity(entity, now=BASE).membership == expected


def test_reply_keyboard_control_types_are_not_lost():
    hidden = normalize_markup(types.ReplyKeyboardHide())
    forced = normalize_markup(types.ReplyKeyboardForceReply())
    assert hidden is not None and hidden.type == "hide"
    assert forced is not None and forced.type == "force_reply"
