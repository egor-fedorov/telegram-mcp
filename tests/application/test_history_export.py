import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Entity, ToolError
from tests.support.fakes import BASE, FakeBackend, msg
from tests.support.incremental import incremental_fixture


def records(result):
    assert result.export is not None
    return [
        json.loads(line)
        for line in Path(result.export.local_path).read_text(encoding="utf-8").splitlines()
    ]


async def test_history_period_is_frozen_across_restart_and_parts(tmp_path):
    backend = FakeBackend([msg("1", mid) for mid in range(1, 250)])
    settings = Settings(allowed_chat_ids=["1"], rpc_pages_per_call=2)
    async with incremental_fixture(tmp_path, backend=backend, settings=settings) as env:
        first = await env.service.export_history(
            chat_ref="1",
            date_from=BASE + timedelta(minutes=2),
            date_to=BASE + timedelta(minutes=220),
        )
        assert not first.complete and first.exported_count == 100
        first_rows = records(first)
        assert first_rows[0]["record"] == "manifest" and first_rows[0]["schema_version"] == 1
        ids = [
            row["message"]["message_ref"]["message_id"]
            for row in first_rows
            if row["record"] == "message"
        ]
        token, upper = first.next_checkpoint, first.upper_message_id
        clock = env.clock
    backend.stored_messages.append(msg("1", 250))
    clock.now += timedelta(days=1)
    async with incremental_fixture(
        tmp_path, backend=backend, settings=settings, clock=clock
    ) as env:
        for _ in range(5):
            part = await env.service.export_history(checkpoint=token)
            assert part.upper_message_id == upper and part.date_to == BASE + timedelta(minutes=220)
            ids.extend(
                row["message"]["message_ref"]["message_id"]
                for row in records(part)
                if row["record"] == "message"
            )
            token = part.next_checkpoint
            if part.complete:
                break
        assert ids == list(range(2, 220))
        assert part.complete and token is None


async def test_history_parts_stop_at_record_limit(tmp_path):
    backend = FakeBackend([msg("1", mid) for mid in range(1, 1003)])
    async with incremental_fixture(tmp_path, backend=backend) as env:
        first = await env.service.export_history(chat_ref="1")
        assert first.exported_count == 1000 and not first.complete
        second = await env.service.export_history(checkpoint=first.next_checkpoint)
        assert second.complete and second.exported_count == 2


@pytest.mark.parametrize("format", ["jsonl", "markdown"])
async def test_whole_unicode_records_resume_at_byte_limit(tmp_path, monkeypatch, format):
    monkeypatch.setattr("telegram_mcp.application.history_export.MAX_EXPORT_BYTES", 12000)
    backend = FakeBackend([msg("1", mid, "😀 résumé\n" * 100) for mid in range(1, 12)])
    async with incremental_fixture(tmp_path, backend=backend) as env:
        page = await env.service.export_history(chat_ref="1", format=format)
        count = 0
        for _ in range(20):
            assert page.export is not None and page.export.size <= 12000
            content, mime = await env.service.read_export_resource(page.export.resource_uri)
            assert "😀 résumé\n" in content or "😀 résumé\\n" in content
            assert mime == ("application/x-ndjson" if format == "jsonl" else "text/markdown")
            count += page.exported_count
            if page.complete:
                break
            page = await env.service.export_history(checkpoint=page.next_checkpoint, format=format)
        assert count == 11 and page.complete


async def test_full_text_entities_and_source_links_survive_export(tmp_path):
    full = "😀\nMixed `code` & <html>\n" * 3000
    message = msg("1", 1, full)
    message.url = "https://t.me/source/1"
    message.entities = [Entity(type="MessageEntityBold", offset=0, length=2)]
    async with incremental_fixture(tmp_path, backend=FakeBackend([message])) as env:
        result = await env.service.export_history(chat_ref="1")
        row = records(result)[1]["message"]
        assert row["text"] == full and row["text_total_length"] == len(full)
        assert row["entities"] == [
            entity.model_dump(exclude_none=True) for entity in message.entities
        ]
        assert row["url"] == message.url and "chat" not in row


async def test_protected_messages_are_explicit_omissions_and_resource_access_is_fresh(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        env.backend.protected_message_ids.add(2)
        result = await env.service.export_history(chat_ref="1")
        assert result.complete and result.exported_count == 2 and result.omitted_count == 1
        assert [r["record"] for r in records(result)] == [
            "manifest",
            "message",
            "omitted",
            "message",
            "summary",
        ]
        assert result.export is not None
        uri = result.export.resource_uri
        env.settings.allowed_chat_ids.clear()
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.read_export_resource(uri)
        env.settings.allowed_chat_ids.append("1")
        env.backend.export_source_error = ToolError("EXPORT_PROTECTED", "Protected")
        with pytest.raises(ToolError, match="EXPORT_PROTECTED"):
            await env.service.read_export_resource(uri)
        with pytest.raises(ToolError, match="EXPORT_PROTECTED"):
            await env.service.export_history(chat_ref="1")


async def test_changes_export_omits_protected_content(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.backend.stored_messages.append(msg("1", 4, "do not save"))
        env.backend.protected_message_ids.add(4)
        result = await env.service.get_changes(checkpoint=start.next_checkpoint, output="jsonl")
        assert result.complete and result.returned_count == result.omitted_count == 1
        assert result.export is not None
        content, _ = await env.service.read_export_resource(result.export.resource_uri)
        assert "do not save" not in content and '"record":"omitted"' in content


async def test_error_after_a_page_preserves_exported_progress(tmp_path, monkeypatch):
    backend = FakeBackend([msg("1", mid) for mid in range(1, 205)])
    original = backend.read_incremental
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ToolError("CONNECTION_ERROR", "Disconnected")
        return await original(*args, **kwargs)

    monkeypatch.setattr(backend, "read_incremental", fail_second)
    async with incremental_fixture(tmp_path, backend=backend) as env:
        first = await env.service.export_history(chat_ref="1")
        assert first.exported_count == 100 and first.next_checkpoint and not first.complete
        assert first.issues[0].code == "CONNECTION_ERROR" and calls == 2
        second = await env.service.export_history(checkpoint=first.next_checkpoint)
        assert second.exported_count == 104 and second.complete and second.issues == []
        assert records(second)[1]["message"]["message_ref"]["message_id"] == 101


async def test_initial_head_failure_is_continuable_without_an_export(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        env.backend.faults["1"] = ToolError("CONNECTION_ERROR", "Offline")
        failed = await env.service.export_history(chat_ref="1")
        assert not failed.complete and failed.next_checkpoint and failed.export is None
        env.backend.faults.clear()
        recovered = await env.service.export_history(checkpoint=failed.next_checkpoint)
        assert recovered.complete and recovered.exported_count == 3


async def test_completed_empty_range_still_has_explicit_manifest(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        empty = await env.service.export_history(chat_ref="1", date_to=BASE)
        assert empty.complete and not empty.exported_count and empty.next_checkpoint is None
        assert [row["record"] for row in records(empty)] == ["manifest", "summary"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"chat_ref": "1", "date_from": datetime(2026, 1, 1)},
        {"chat_ref": "1", "date_from": BASE, "date_to": BASE},
        {"checkpoint": "bad", "date_to": BASE},
        {"chat_ref": "1", "format": "pdf"},
    ],
)
async def test_invalid_export_arguments(tmp_path, arguments):
    async with incremental_fixture(tmp_path) as env:
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await env.service.export_history(**arguments)


async def test_disk_failure_does_not_consume_input_checkpoint(tmp_path, monkeypatch):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.checkpoints.maximum = 2
        await env.service.get_changes(chat_ref="1")
        env.backend.stored_messages.append(msg("1", 4))
        save = env.exports.save

        async def fail(*args, **kwargs):
            raise ToolError("LOCAL_IO_ERROR", "Disk is full")

        monkeypatch.setattr(env.exports, "save", fail)
        with pytest.raises(ToolError, match="LOCAL_IO_ERROR"):
            await env.service.get_changes(checkpoint=start.next_checkpoint, output="jsonl")
        monkeypatch.setattr(env.exports, "save", save)
        retry = await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert [c.message_ref.message_id for c in retry.changes] == [4]
