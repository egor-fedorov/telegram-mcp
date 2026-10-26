import json
from datetime import timedelta

import pytest

from telegram_mcp.domain.changes import content_revision
from telegram_mcp.domain.exporting import ExportPart
from telegram_mcp.domain.models import Entity, ToolError
from tests.support.fakes import BASE, msg
from tests.support.message_metadata import rich_message


def test_revision_ignores_delivery_and_volatile_counters_but_detects_content():
    message = rich_message(1)
    original = content_revision(message, "document:1")
    message.view_count = 999
    message.reply_count = 500
    message.forward_count = 400
    message.reactions = None
    message.chat.title = "Renamed"
    message.sender.name = "New profile"
    message.text_total_length = 4000
    message.next_text_offset = 12
    assert message.poll is not None
    message.poll.results = None
    assert content_revision(message, "document:1") == original
    assert content_revision(message, "document:2") != original
    message.edited_at = BASE + timedelta(hours=1)
    assert content_revision(message, "document:1") != original


@pytest.mark.parametrize(
    "field,value", [("text", "Revised"), ("entities", [Entity(type="bold", offset=0, length=1)])]
)
def test_revision_detects_text_and_entities(field, value):
    message = msg("1", 1)
    revision = content_revision(message)
    setattr(message, field, value)
    assert content_revision(message) != revision


def test_markdown_treats_source_content_as_data_and_preserves_full_text():
    text = "<script>alert(1)</script>\n```\n# Fake section\n``````\n😀"
    message = msg("1", 1, text)
    message.chat.title = "<b>Title</b>\n# Fake title"
    message.url = "https://t.me/source/1"
    part = ExportPart(
        "markdown",
        message.chat,
        kind="history",
        generated_at=BASE,
        upper_id=1,
        date_from=None,
        date_to=BASE,
        max_bytes=20000,
    )
    assert part.add(message)
    rendered = part.finish({"complete": True}).decode()
    assert "```````text\n" + text + "\n```````" in rendered
    assert "[Original message](<https://t.me/source/1>)" in rendered
    assert "&lt;b&gt;Title&lt;/b&gt;" in rendered


def test_jsonl_is_versioned_and_does_not_split_a_record():
    message = msg("1", 1, "😀" * 500)
    part = ExportPart(
        "jsonl",
        message.chat,
        kind="history",
        generated_at=BASE,
        upper_id=1,
        date_from=None,
        date_to=BASE,
        max_bytes=6000,
    )
    assert part.add(message)
    assert not part.add(message)
    rows = [json.loads(line) for line in part.finish({"complete": False}).splitlines()]
    assert rows[0]["schema_version"] == 1
    assert rows[1]["message"]["text"] == message.text
    assert rows[-1]["complete"] is False


def test_oversize_single_record_is_explicit():
    message = msg("1", 1, "x" * 10000)
    part = ExportPart(
        "jsonl",
        message.chat,
        kind="history",
        generated_at=BASE,
        upper_id=1,
        date_from=None,
        date_to=BASE,
        max_bytes=5000,
    )
    with pytest.raises(ToolError, match="EXPORT_RECORD_TOO_LARGE"):
        part.add(message)
