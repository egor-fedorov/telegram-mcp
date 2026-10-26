import json
from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, Entity, ToolError
from tests.support.fakes import FakeBackend, msg
from tests.support.incremental import incremental_fixture


async def test_baseline_restart_fixed_head_and_old_token_replay(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        assert start.complete and start.changes == [] and start.tracked_count == 3
        token = start.next_checkpoint
    backend = FakeBackend([msg("1", mid) for mid in range(1, 10)])
    async with incremental_fixture(tmp_path, backend=backend) as env:
        first = await env.service.get_changes(checkpoint=token, limit=2)
        assert [c.message_ref.message_id for c in first.changes] == [4, 5]
        assert not first.complete and first.upper_message_id == 9
        replay = await env.service.get_changes(checkpoint=token, limit=2)
        assert [(c.message_ref, c.revision) for c in replay.changes] == [
            (c.message_ref, c.revision) for c in first.changes
        ]
        backend.stored_messages.append(msg("1", 10))
        continuation = first.next_checkpoint
    async with incremental_fixture(tmp_path, backend=backend) as env:
        rest = await env.service.get_changes(checkpoint=continuation, limit=100)
        assert rest.complete
        assert [c.message_ref.message_id for c in rest.changes] == [6, 7, 8, 9]
        later = await env.service.get_changes(checkpoint=rest.next_checkpoint)
        assert [c.message_ref.message_id for c in later.changes] == [10]


async def test_updates_unavailable_restored_and_counter_changes(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.backend.stored_messages[0].text = "Правка 😀"
        env.backend.stored_messages[0].entities = [
            Entity(type="MessageEntityBold", offset=7, length=2)
        ]
        env.backend.stored_messages[1].view_count = 500
        removed = env.backend.stored_messages.pop()
        page = await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert [(c.kind, c.message_ref.message_id) for c in page.changes] == [
            ("updated", 1),
            ("unavailable", 3),
        ]
        assert page.complete and page.rechecked_message_ids == [1, 2, 3]
        unchanged = await env.service.get_changes(checkpoint=page.next_checkpoint)
        assert unchanged.changes == []
        env.backend.stored_messages.append(removed)
        restored = await env.service.get_changes(checkpoint=unchanged.next_checkpoint)
        assert [(c.kind, c.message_ref.message_id) for c in restored.changes] == [("updated", 3)]


async def test_rechecks_survive_pagination_and_only_track_last_hundred(tmp_path):
    backend = FakeBackend([msg("1", mid) for mid in range(1, 106)])
    async with incremental_fixture(tmp_path, backend=backend) as env:
        start = await env.service.get_changes(chat_ref="1")
        assert start.tracked_count == 100
        for message in backend.stored_messages:
            message.text = "Changed"
        found = []
        token = start.next_checkpoint
        for _ in range(20):
            page = await env.service.get_changes(checkpoint=token, limit=9)
            found.extend(c.message_ref.message_id for c in page.changes)
            token = page.next_checkpoint
            if page.complete:
                break
        assert found == list(range(6, 106))
        assert page.complete
        assert all(len(ids) <= 100 for _, ids in backend.batch_reads)


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"])
async def test_temporary_error_keeps_position_and_clears_after_recovery(tmp_path, code):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.backend.stored_messages.append(msg("1", 4))
        env.backend.message_faults["1"] = ToolError(
            code, "Temporary failure", 30 if code == "RATE_LIMITED" else None
        )
        failed = await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert not failed.complete and failed.next_checkpoint
        assert failed.issues[0].code == code
        assert env.backend.backend_calls.count("observe_messages") == 1
        token = failed.next_checkpoint
        clock = env.clock
        backend = env.backend
    backend.message_faults.clear()
    backend.backend_calls.clear()
    async with incremental_fixture(tmp_path, backend=backend, clock=clock) as env:
        if code == "RATE_LIMITED":
            until = env.checkpoints.retry_at("session")
            for args in (
                {"checkpoint": token},
                {"checkpoint": start.next_checkpoint},
                {"chat_ref": "1"},
            ):
                clock.now += timedelta(milliseconds=100)
                with pytest.raises(ToolError, match="RATE_LIMITED"):
                    await env.service.get_changes(
                        chat_ref=args.get("chat_ref"), checkpoint=args.get("checkpoint")
                    )
                assert backend.backend_calls == []
                assert env.checkpoints.retry_at("session") == until
            clock.now += timedelta(seconds=31)
        recovered = await env.service.get_changes(checkpoint=token)
        assert recovered.complete and not recovered.issues
        assert [c.message_ref.message_id for c in recovered.changes] == [4]


async def test_empty_budget_page_and_failed_authorization_do_not_consume_checkpoint(tmp_path):
    settings = Settings(allowed_chat_ids=["1"], rpc_pages_per_call=1)
    async with incremental_fixture(tmp_path, settings=settings) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.backend.stored_messages.append(msg("1", 4))
        empty = await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert not empty.changes and not empty.complete and empty.next_checkpoint
        env.backend.authorization_fault = ToolError("CONNECTION_ERROR", "Offline")
        with pytest.raises(ToolError, match="CONNECTION_ERROR"):
            await env.service.get_changes(checkpoint=empty.next_checkpoint)
        env.backend.authorization_fault = None
        rechecked = await env.service.get_changes(checkpoint=empty.next_checkpoint)
        final = await env.service.get_changes(checkpoint=rechecked.next_checkpoint)
        assert final.complete and final.changes[0].message_ref.message_id == 4


async def test_unicode_inline_budget_and_full_export_are_independent(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        full = "😀é\n" * 6000
        env.backend.stored_messages.append(msg("1", 4, full))
        inline = await env.service.get_changes(checkpoint=start.next_checkpoint)
        message = inline.changes[0].message
        assert message is not None and message.text == full[:12000]
        assert message.next_text_offset == 12000 and message.text_total_length == len(full)
        exported = await env.service.get_changes(checkpoint=start.next_checkpoint, output="jsonl")
        assert exported.changes == [] and exported.export is not None
        text, mime = await env.service.read_export_resource(exported.export.resource_uri)
        records = [json.loads(line) for line in text.splitlines()]
        assert records[1]["message"]["text"] == full
        assert mime == "application/x-ndjson"


async def test_explicit_public_scope_is_canonical_and_revocable(tmp_path):
    chat = Chat(id="-1001", title="Channel", username="original", type="channel")
    backend = FakeBackend([], chats=[], external_chats=[chat])
    settings = Settings(allow_public_channel_reads=True)
    async with incremental_fixture(tmp_path, backend=backend, settings=settings) as env:
        start = await env.service.get_changes(chat_ref="@original")
        chat.username = "renamed"
        page = await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert page.chat_id == "-1001" and page.complete
        chat.username = None
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.get_changes(checkpoint=page.next_checkpoint)
        chat.username = "renamed"
        settings.allow_public_channel_reads = False
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.get_changes(checkpoint=page.next_checkpoint)


async def test_allowlist_revocation_and_other_authorization_are_rejected(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.settings.allowed_chat_ids.clear()
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.get_changes(checkpoint=start.next_checkpoint)
        env.settings.allowed_chat_ids.append("1")
        env.backend.binding = "another-authorization"
        with pytest.raises(ToolError, match="CHECKPOINT_MISMATCH"):
            await env.service.get_changes(checkpoint=start.next_checkpoint)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"chat_ref": ""},
        {"chat_ref": "1", "checkpoint": "bad"},
        {"chat_ref": "1", "limit": 0},
        {"chat_ref": "1", "output": "pdf"},
    ],
)
async def test_invalid_change_arguments(tmp_path, arguments):
    async with incremental_fixture(tmp_path) as env:
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await env.service.get_changes(**arguments)


async def test_empty_chat_baseline_then_new_message(tmp_path):
    backend = FakeBackend([], chats=[Chat(id="1", title="Empty", type="private")])
    async with incremental_fixture(tmp_path, backend=backend) as env:
        start = await env.service.get_changes(chat_ref="1")
        assert start.upper_message_id == 0 and start.complete
        backend.stored_messages.append(msg("1", 10))
        next_page = await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert [c.message_ref.message_id for c in next_page.changes] == [10]


@pytest.mark.parametrize("kind", ["private", "group", "bot", "saved"])
async def test_public_exception_never_grants_other_chat_types(tmp_path, kind):
    chat = Chat(id="1", title="Other", type=kind, username="name")
    async with incremental_fixture(
        tmp_path,
        backend=FakeBackend([], chats=[chat]),
        settings=Settings(allow_public_channel_reads=True),
    ) as env:
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.get_changes(chat_ref="@name")
        assert "recent_observations" not in env.backend.backend_calls


async def test_authorization_revocation_is_not_converted_into_message_unavailability(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.backend.authorization_fault = ToolError("AUTH_REQUIRED", "Session revoked")
        with pytest.raises(ToolError, match="AUTH_REQUIRED"):
            await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert "observe_messages" not in env.backend.backend_calls
        env.backend.authorization_fault = None
        assert (await env.service.get_changes(checkpoint=start.next_checkpoint)).complete


async def test_checkpoint_storage_never_contains_message_bodies(tmp_path):
    unique = "A_PRIVATE_MESSAGE_THAT_MUST_NOT_BE_PERSISTED_вот_так"
    backend = FakeBackend([msg("1", 1, unique)])
    async with incremental_fixture(tmp_path, backend=backend) as env:
        start = await env.service.get_changes(chat_ref="1")
        backend.stored_messages.append(msg("1", 2, unique * 2))
        await env.service.get_changes(checkpoint=start.next_checkpoint)
        path = env.checkpoints.path
    assert unique.encode() not in path.read_bytes()


async def test_recheck_network_timeout_and_cancellation_leave_original_position(
    tmp_path, monkeypatch
):
    import asyncio

    from telegram_mcp.application.incremental import ReadBudget
    from telegram_mcp.application.ports import ExecutionLimits

    async with incremental_fixture(tmp_path) as env:
        start = await env.service.get_changes(chat_ref="1")
        original = env.backend.observe_messages
        entered = asyncio.Event()

        async def wait_forever(*args):
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(env.backend, "observe_messages", wait_forever)
        task = asyncio.create_task(env.service.get_changes(checkpoint=start.next_checkpoint))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        def short_budget(clock, limits):
            budget = ReadBudget(clock, ExecutionLimits())
            budget.deadline = clock.monotonic() + 0.01
            return budget

        monkeypatch.setattr("telegram_mcp.application.changes.ReadBudget", short_budget)
        failed = await env.service.get_changes(checkpoint=start.next_checkpoint)
        assert (
            not failed.complete
            and failed.next_checkpoint
            and failed.issues[0].code == "CONNECTION_ERROR"
        )
        monkeypatch.setattr(env.backend, "observe_messages", original)
        recovered = await env.service.get_changes(checkpoint=failed.next_checkpoint)
        assert recovered.complete and recovered.changes == []
