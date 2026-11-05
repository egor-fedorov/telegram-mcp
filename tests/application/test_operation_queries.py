"""Account-bound recovery and stable, bounded pages of the MCP operation journal."""

import asyncio
from datetime import timedelta

import pytest

from telegram_mcp.domain.models import ToolError
from tests.support.fakes import BASE
from tests.support.outbound import approve, outbound_fixture


async def test_lost_response_can_be_recovered_by_key_without_resolving_or_sending(
    tmp_path, monkeypatch
):
    async with outbound_fixture(tmp_path) as env:
        receipt = await env.service.send_message("1", "Synthetic", "unique", confirm=approve)
        telegram = env.telegram
    async with outbound_fixture(tmp_path, telegram=telegram) as env:
        env.settings.allowed_chat_ids = env.settings.allowed_write_chat_ids = []

        async def forbidden(*args, **kwargs):
            raise AssertionError("Journal queries must not resolve peers or read history")

        monkeypatch.setattr(env.backend, "resolve_chat", forbidden)
        monkeypatch.setattr(env.backend, "catalog", forbidden)
        recovered = await env.service.get_send_status(idempotency_key="unique")
        assert recovered == receipt and len(telegram.send_calls) == 1
        assert (await env.service.list_operations()).operations[
            0
        ].operation_id == receipt.operation_id
        telegram.account = "other"
        assert not (await env.service.list_operations()).operations
        with pytest.raises(ToolError, match="OPERATION_UNAVAILABLE"):
            await env.service.get_send_status(idempotency_key="unique")


@pytest.mark.parametrize(
    "arguments", [{}, {"operation_id": "a", "idempotency_key": "b"}, {"idempotency_key": " "}]
)
async def test_lookup_requires_exactly_one_valid_selector(tmp_path, arguments):
    async with outbound_fixture(tmp_path) as env:
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await env.service.get_send_status(**arguments)


async def test_pages_filter_creation_dates_and_exclude_later_insertions(tmp_path):
    async with outbound_fixture(tmp_path) as env:
        for i in range(5):
            env.clock.now = BASE + timedelta(minutes=i)
            await env.service.send_message(
                "1" if i % 2 == 0 else "2", f"Body {i}", f"key-{i}", confirm=approve
            )
        first = await env.service.list_operations(limit=2)
        assert [x.idempotency_key for x in first.operations] == ["key-4", "key-3"]
        assert first.scope == "mcp_journal" and "Body" not in first.model_dump_json()
        await env.service.send_message("1", "Inserted later", "later", confirm=approve)
        second = await env.service.list_operations(limit=2, cursor=first.next_cursor)
        third = await env.service.list_operations(limit=2, cursor=second.next_cursor)
        assert [x.idempotency_key for x in second.operations + third.operations] == [
            "key-2",
            "key-1",
            "key-0",
        ]
        assert third.next_cursor is None
        filtered = await env.service.list_operations(
            chat_id="1",
            status="sent",
            kind="message",
            date_from=BASE,
            date_to=BASE + timedelta(minutes=4),
        )
        assert [x.idempotency_key for x in filtered.operations] == ["key-2", "key-0"]
        assert filtered.operations[-1].created_at == BASE
        with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
            await env.service.list_operations(chat_id="2", cursor=first.next_cursor)
        env.telegram.account = "another"
        with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
            await env.service.list_operations(cursor=first.next_cursor)


@pytest.mark.parametrize(
    "filters",
    [
        {"limit": 0},
        {"chat_id": "@name"},
        {"status": "bad"},
        {"date_from": BASE.replace(tzinfo=None)},
        {"date_from": BASE, "date_to": BASE},
    ],
)
async def test_invalid_journal_filters_are_rejected(tmp_path, filters):
    async with outbound_fixture(tmp_path) as env:
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await env.service.list_operations(**filters)


async def test_status_filters_distinguish_active_pending_from_interrupted_dispatch(
    tmp_path, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()
    async with outbound_fixture(tmp_path) as env:
        original = env.telegram.send

        async def held(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        monkeypatch.setattr(env.telegram, "send", held)
        task = asyncio.create_task(env.service.send_message("1", "Waiting", "key", confirm=approve))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            assert (await env.service.list_operations(status="pending")).operations[
                0
            ].status == "pending"
            assert not (await env.service.list_operations(status="unknown")).operations
        finally:
            release.set()
            await task
        operation = await env.store.operation("101", "key")
        operation.receipt.status = "pending"
        await env.store.save_operation(operation)
        assert (await env.service.get_send_status(idempotency_key="key")).status == "unknown"
        assert (await env.service.list_operations(status="unknown")).operations[
            0
        ].status == "unknown"
        assert not (await env.service.list_operations(status="pending")).operations
