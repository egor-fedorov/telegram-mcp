import asyncio
from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, Folder, ToolError
from tests.support.fakes import BASE, FakeBackend, msg
from tests.support.incremental import incremental_fixture


async def test_pending_batch_survives_lost_response_restart_and_duplicate_ack(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        created = await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        assert created.source_count == 1
        first = await env.service.get_watch_changes("jobs", limit=2)
        assert first.batch_id and first.returned_count == 2 and not first.complete
        again = await env.service.get_watch_changes("jobs", limit=100)
        assert again == first
        persisted = env.watches.get("jobs", env.backend.binding)
        assert persisted is not None
        assert persisted.position.sources["1"].position == 0
    async with incremental_fixture(tmp_path) as env:
        found = await env.service.list_watches()
        assert found.watches[0].pending_batch_id == first.batch_id
        assert await env.service.get_watch_changes("jobs") == first
        await env.service.ack_watch_changes("jobs", first.batch_id)
        await env.service.ack_watch_changes("jobs", first.batch_id)
        next_page = await env.service.get_watch_changes("jobs")
        assert [c.message_ref.message_id for c in next_page.changes] == [3]
        await env.service.ack_watch_changes("jobs", first.batch_id)
        assert (await env.service.get_watch_changes("jobs")).batch_id == next_page.batch_id
        assert next_page.batch_id is not None
        await env.service.ack_watch_changes("jobs", next_page.batch_id)
        with pytest.raises(ToolError, match="WATCH_BATCH_MISMATCH"):
            await env.service.ack_watch_changes("jobs", first.batch_id)
        assert (await env.service.get_watch_changes("jobs")).changes == []


async def test_default_start_is_creation_time_and_repeated_create_does_not_reset(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        created = await env.service.create_watch("jobs", chat_refs=["1"])
        env.clock.now += timedelta(hours=1)
        fresh = msg("1", 4, "New vacancy")
        fresh.sent_at = env.clock.now
        env.backend.stored_messages.append(fresh)
        assert (
            await env.service.create_watch("jobs", chat_refs=["1"])
        ).started_at == created.started_at
        with pytest.raises(ToolError, match="WATCH_CONFLICT"):
            await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        page = await env.service.get_watch_changes("jobs")
        assert [c.message_ref.message_id for c in page.changes] == [4]
        assert await env.service.get_watch_changes("jobs") == page


async def test_default_start_includes_new_posts_with_same_telegram_second(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        env.clock.now = env.clock.now.replace(microsecond=123456)
        await env.service.create_watch("jobs", chat_refs=["1"])
        fresh = msg("1", 4)
        fresh.sent_at = env.clock.now.replace(microsecond=0)
        env.backend.stored_messages.append(fresh)
        page = await env.service.get_watch_changes("jobs")
        assert [change.message_ref.message_id for change in page.changes] == [4]


async def test_pending_folder_batch_is_preserved_during_catalog_and_replay_timeout(
    tmp_path, monkeypatch
):
    backend = FakeBackend([msg("1", 1), msg("2", 1)])
    backend.folders = [Folder(id=1, title="Jobs", position=0, included_chat_ids=["1", "2"])]
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        page = await env.service.get_watch_changes("jobs")
        before = env.watches.get("jobs", env.backend.binding)
        catalog = backend.catalog

        async def unavailable():
            raise ToolError("CATALOG_LOADING", "Loading", 1)

        monkeypatch.setattr(backend, "catalog", unavailable)
        with pytest.raises(ToolError, match="CATALOG_LOADING"):
            await env.service.get_watch_changes("jobs")
        assert env.watches.get("jobs", env.backend.binding) == before
        monkeypatch.setattr(backend, "catalog", catalog)
        check = backend.check_export_source

        async def slow_check(cid):
            await check(cid)
            env.clock.time += 36

        monkeypatch.setattr(backend, "check_export_source", slow_check)
        with pytest.raises(ToolError, match="CONNECTION_ERROR"):
            await env.service.get_watch_changes("jobs")
        assert env.watches.get("jobs", env.backend.binding) == before
        monkeypatch.setattr(backend, "check_export_source", check)
        assert await env.service.get_watch_changes("jobs") == page


async def test_single_page_budget_advances_after_head_without_losing_changes(tmp_path):
    settings = Settings(allowed_chat_ids=["1"], rpc_pages_per_call=1)
    async with incremental_fixture(tmp_path, settings=settings) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs", limit=2)
        assert first.batch_id is None and not first.complete
        found = []
        for _ in range(3):
            page = await env.service.get_watch_changes("jobs", limit=2)
            assert page.progress.rpc_pages_this_call <= 1
            found.extend(c.message_ref.message_id for c in page.changes)
            if page.batch_id:
                assert await env.service.get_watch_changes("jobs") == page
                await env.service.ack_watch_changes("jobs", page.batch_id)
            if page.complete:
                break
        assert page.complete and found == [1, 2, 3]


async def test_watch_text_budget_preserves_unicode_offsets_and_next_page(tmp_path):
    original = "😀界" * 10000
    backend = FakeBackend([msg("1", mid, original) for mid in range(1, 12)])
    async with incremental_fixture(tmp_path, backend=backend) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        found = []
        for _ in range(3):
            page = await env.service.get_watch_changes("jobs")
            messages = [change.message for change in page.changes if change.message is not None]
            assert sum(len(message.text) for message in messages) <= 100000
            for message in messages:
                assert message.text == original[: len(message.text)]
                assert message.text_total_length == len(original)
                assert message.next_text_offset == len(message.text)
                found.append(message.message_ref.message_id)
            assert page.batch_id
            await env.service.ack_watch_changes("jobs", page.batch_id)
            if page.complete:
                break
        assert page.complete and found == list(range(1, 12))


async def test_folder_of_fifty_sources_produces_early_results_and_needs_no_tokens(tmp_path):
    chats = [Chat(id=str(i), title=f"Source {i}", type="channel") for i in range(1, 51)]
    backend = FakeBackend([msg(c.id, 1) for c in chats], chats)
    backend.folders = [
        Folder(id=1, position=0, title="Jobs", included_chat_ids=[c.id for c in chats])
    ]
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        found = set()
        for _ in range(30):
            page = await env.service.get_watch_changes("jobs")
            assert page.progress.rpc_pages_this_call <= 12
            assert page.changes  # No all-source head warmup.
            found.update(c.message_ref.chat_id for c in page.changes)
            assert page.batch_id is not None
            await env.service.ack_watch_changes("jobs", page.batch_id)
            if page.complete:
                break
        assert found == {c.id for c in chats}
        assert page.complete


async def test_new_folder_member_starts_at_previous_completed_pass_and_returning_member_resumes(
    tmp_path,
):
    backend = FakeBackend(
        [msg("1", 1)],
        chats=[
            Chat(id="1", title="One", type="channel"),
            Chat(id="2", title="Two", type="channel"),
        ],
    )
    backend.folders = [Folder(id=1, position=0, title="Jobs", included_chat_ids=["1"])]
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1])
        first = await env.service.get_watch_changes("jobs")
        assert first.complete and first.batch_id is None
        boundary = env.clock.now
        env.clock.now += timedelta(hours=1)
        old, new = msg("2", 1), msg("2", 2)
        old.sent_at, new.sent_at = boundary - timedelta(seconds=1), boundary + timedelta(minutes=1)
        backend.stored_messages.extend([old, new])
        backend.folders[0].included_chat_ids.append("2")
        page = await env.service.get_watch_changes("jobs")
        assert [c.message_ref.key for c in page.changes] == ["2:2"]
        assert page.batch_id is not None
        await env.service.ack_watch_changes("jobs", page.batch_id)
        backend.folders[0].included_chat_ids.remove("2")
        assert (await env.service.get_watch_changes("jobs")).complete
        returned = msg("2", 3)
        returned.sent_at = env.clock.now
        backend.stored_messages.append(returned)
        backend.folders[0].included_chat_ids.append("2")
        page = await env.service.get_watch_changes("jobs")
        assert [c.message_ref.key for c in page.changes] == ["2:3"]


async def test_access_revocation_and_protection_prevent_cached_delivery_without_advancement(
    tmp_path,
):
    async with incremental_fixture(tmp_path) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs")
        before = env.watches.get("jobs", env.backend.binding)
        env.settings.allowed_chat_ids = []
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.get_watch_changes("jobs")
        assert env.watches.get("jobs", env.backend.binding) == before
        env.settings.allowed_chat_ids = ["1"]
        env.backend.export_source_error = ToolError("EXPORT_PROTECTED", "Protected now")
        with pytest.raises(ToolError, match="EXPORT_PROTECTED"):
            await env.service.get_watch_changes("jobs")
        env.backend.export_source_error = None
        assert await env.service.get_watch_changes("jobs") == first


async def test_protected_messages_are_explicit_omissions_and_quota_failure_preserves_input(
    tmp_path,
):
    async with incremental_fixture(tmp_path) as env:
        env.backend.protected_message_ids = {2}
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        before = env.watches.get("jobs", env.backend.binding)
        assert before is not None
        env.watches.max_bytes = len(before.model_dump_json().encode())
        with pytest.raises(ToolError, match="WATCH_STORAGE_FULL"):
            await env.service.get_watch_changes("jobs")
        assert env.watches.get("jobs", env.backend.binding) == before
        env.watches.max_bytes = 64 * 1024 * 1024
        page = await env.service.get_watch_changes("jobs")
        assert [c.message_ref.message_id for c in page.changes] == [1, 3]
        assert page.omitted_count == 1 and page.issues[0].code == "EXPORT_PROTECTED"
        stored = env.watches.get("jobs", env.backend.binding)
        assert stored is not None and stored.pending is not None
        assert all(c.message_ref.message_id != 2 for c in stored.pending.result.changes)


async def test_source_failure_does_not_starve_healthy_source_and_floodwait_survives_restart(
    tmp_path,
):
    backend = FakeBackend([msg("1", 1), msg("2", 1)])
    settings = Settings(allowed_chat_ids=["1", "2"])
    async with incremental_fixture(tmp_path, backend=backend, settings=settings) as env:
        await env.service.create_watch("jobs", chat_refs=["1", "2"], date_from=BASE)
        backend.faults["1"] = ToolError("CONNECTION_ERROR", "Unavailable")
        page = await env.service.get_watch_changes("jobs")
        assert not page.complete and [c.message_ref.key for c in page.changes] == ["2:1"]
        assert page.batch_id is not None
        await env.service.ack_watch_changes("jobs", page.batch_id)
        fresh = msg("2", 2)
        backend.stored_messages.append(fresh)
        page = await env.service.get_watch_changes("jobs")
        assert [c.message_ref.key for c in page.changes] == ["2:2"]
        assert page.batch_id is not None
        await env.service.ack_watch_changes("jobs", page.batch_id)
        backend.faults["1"] = ToolError("RATE_LIMITED", "Wait", 60)
        failed = await env.service.get_watch_changes("jobs")
        assert any(issue.code == "RATE_LIMITED" for issue in failed.issues)
    async with incremental_fixture(tmp_path, backend=backend, settings=settings) as env:
        before = list(backend.backend_calls)
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await env.service.get_watch_changes("jobs")
        assert backend.backend_calls == before
        env.clock.now += timedelta(seconds=61)
        backend.faults.clear()
        page = await env.service.get_watch_changes("jobs")
        assert [c.message_ref.key for c in page.changes] == ["1:1"]


async def test_parallel_watch_calls_replay_one_batch_and_independent_m4_calls_overlap(
    tmp_path, monkeypatch
):
    async with incremental_fixture(tmp_path) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        entered, release = asyncio.Event(), asyncio.Event()
        original = env.backend.recent_observations

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(env.backend, "recent_observations", held)
        first = asyncio.create_task(env.service.get_watch_changes("jobs"))
        await entered.wait()
        second = asyncio.create_task(env.service.get_watch_changes("jobs"))
        # A different M4 request reaches Telegram while the watch is still waiting.
        entered.clear()
        independent = asyncio.create_task(env.service.get_changes(chat_ref="1"))
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        pages = await asyncio.gather(first, second)
        assert pages[0] == pages[1] and pages[0].batch_id is not None
        assert (await independent).baseline_created


async def test_cancellation_delete_and_authorization_binding(tmp_path, monkeypatch):
    async with incremental_fixture(tmp_path) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        before = env.watches.get("jobs", env.backend.binding)
        original = env.backend.read_incremental
        entered = asyncio.Event()

        async def held(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        monkeypatch.setattr(env.backend, "read_incremental", held)
        task = asyncio.create_task(env.service.get_watch_changes("jobs"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.watches.get("jobs", env.backend.binding) == before
        monkeypatch.setattr(env.backend, "read_incremental", original)
        env.backend.binding = "another-authorization"
        assert (await env.service.list_watches()).watches == []
        with pytest.raises(ToolError, match="WATCH_NOT_FOUND"):
            await env.service.get_watch_changes("jobs")
        env.backend.binding = "test-authorization"
        page = await env.service.get_watch_changes("jobs")
        assert (await env.service.delete_watch("jobs")).deleted
        assert not (await env.service.delete_watch("jobs")).deleted
        await env.service.create_watch("jobs", chat_refs=["1"])
        with pytest.raises(ToolError, match="WATCH_BATCH_MISMATCH"):
            assert page.batch_id is not None
            await env.service.ack_watch_changes("jobs", page.batch_id)
