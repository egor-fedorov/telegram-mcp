"""Specific authentication notices are omitted without hiding ordinary numeric posts."""

import pytest
from mcp_types import TextContent

from telegram_mcp.adapters.wire import encode
from telegram_mcp.domain.auth_notifications import authentication_notice
from telegram_mcp.domain.changes import Change, content_revision
from telegram_mcp.domain.models import Entity, Message, MessageButton, ReplyMarkup
from telegram_mcp.domain.text import clip
from tests.support.fakes import BASE, msg


@pytest.mark.parametrize(
    "text",
    [
        "Your login code is 12345. Do not share it.",
        "Код для входа в getmatch: 123456",
        "Ваш код авторизации: １２３４５",
        "Код подтверждения: 12\u200b34\u200b56",
        "Sign-in link: https://example.test/login?token=abcdefghi",
        "Войти: https://example.test/?login_token=abcdefghi",
    ],
)
def test_authentication_notice_requires_context_and_credential(text):
    assert authentication_notice(text)
    message = msg("2", 10, text)
    assert message.omission_reason == "AUTH_NOTIFICATION_FILTERED" and message.text == ""
    assert message.entities == [] and message.attachments == [] and message.reply_markup is None


@pytest.mark.parametrize(
    "text",
    [
        "Salary 123456 RUB, apply with your CV",
        "Код вакансии 12345, зарплата 200000",
        "We build login systems. Review Python code. Salary 123456.",
        "Пример кода на Python, 12345 строк",
        "Login code is sent separately",
        "123456",
        "Join https://example.test/?code=123456",
        "Код региона 1234",
    ],
)
def test_ordinary_posts_and_links_are_not_filtered(text):
    assert not authentication_notice(text)
    assert msg("2", 1, text).text == text


def test_credential_in_buttons_is_filtered_before_slicing_and_wire_delivery():
    message = msg("2", 1, "Войти в аккаунт").model_copy(
        update={
            "reply_markup": ReplyMarkup(
                type="inline",
                rows=[
                    [
                        MessageButton(
                            button_id="opaque",
                            type="url",
                            text="Login",
                            url="https://example.test/?token=abcdefghi",
                        )
                    ]
                ],
            ),
            "entities": [
                Entity(
                    type="MessageEntityTextUrl",
                    offset=0,
                    length=5,
                    url="https://example.test/?token=abcdefghi",
                )
            ],
            "revision": "credential-derived",
        }
    )
    clipped = clip(message, 2)
    assert clipped.omission_reason and not clipped.text and not clipped.next_text_offset
    block = encode(message).content[0]
    assert isinstance(block, TextContent)
    payload = block.text
    assert "abcdefghi" not in payload and "credential-derived" not in payload
    assert "AUTH_NOTIFICATION_FILTERED" in payload


def test_reopening_persisted_change_removes_old_secret_and_its_hash():
    data = msg("2", 1).model_dump()
    data.update(text="Your verification code: 987654", revision="old-secret-hash")
    change = Change.model_validate(
        dict(
            kind="new",
            message_ref={"chat_id": "2", "message_id": 1},
            revision="old-secret-hash",
            observed_at=BASE,
            message=data,
        )
    )
    assert change.message is not None
    assert change.revision == content_revision(change.message)
    assert (
        "987654" not in change.model_dump_json()
        and "old-secret-hash" not in change.model_dump_json()
    )
    assert Message.model_validate(change.message.model_dump()).omission_reason
