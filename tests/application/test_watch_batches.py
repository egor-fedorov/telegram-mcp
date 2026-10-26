from dataclasses import replace
from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, Folder, ToolError
from tests.support.fakes import BASE, FakeBackend, msg
from tests.support.incremental import incremental_fixture
from tests.support.message_metadata import rich_message


def channels(count):
    chats = [Chat(id=f"-100{i}", title=f"Source {i}", type="channel") for i in range(1, count + 1)]
    backend = FakeBackend([msg(chat.id, 1) for chat in chats], chats)
    backend.folders = [
        Folder(id=1, title="Jobs", position=0, included_chat_ids=[c.id for c in chats])
    ]
    return backend


async def drain(service, *, maximum=200):
    ack = None
    found = []
    for _ in range(maximum):
        page = await service.get_watch_changes("jobs", limit=100, ack_batch_id=ack)
        found.extend(page.changes)
        ack = page.batch_id
        if page.complete:
            if ack:
                await service.ack_watch_changes("jobs", ack)
            return found
    pytest.fail("Watch did not finish within the bounded number of calls")


@pytest.mark.parametrize("count,budget", [(100, 1), (1000, 12)])
async def test_warm_unchanged_channels_need_only_batched_metadata(tmp_path, count, budget):
    backend = channels(count)
    settings = Settings(allowed_folder_ids=[1])
    async with incremental_fixture(tmp_path, backend=backend, settings=settings) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        assert len(await drain(env.service)) == count
        settings.rpc_pages_per_call = budget
        # Execution limits are intentionally immutable after server construction.
        env.service._incremental.limits = replace(
            env.service._incremental.limits, rpc_pages_per_call=budget
        )
        backend.backend_calls.clear()
        backend.peer_requests.clear()
        page = await env.service.get_watch_changes("jobs")
        assert page.complete and page.batch_id is None and not page.changes
        assert len(backend.peer_requests) == count // 100
        assert page.progress.rpc_pages_this_call == count // 100
        assert page.progress.sources_this_call == count
        assert set(backend.backend_calls) <= {
            "checkpoint_binding",
            "catalog",
            "peer_states",
            "authorization",
        }
        assert backend.backend_calls.count("authorization") == 1


async def test_edit_deletion_and_new_message_are_detected_without_head_only_shortcut(tmp_path):
    backend = channels(3)
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        await drain(env.service)
        backend.stored_messages[0].text = "Edited vacancy"
        backend.stored_messages.pop(1)
        backend.stored_messages.append(msg("-1003", 2, "New vacancy"))
        changes = await drain(env.service)
        assert {(c.message_ref.key, c.kind) for c in changes} == {
            ("-1001:1", "updated"),
            ("-1002:1", "unavailable"),
            ("-1003:2", "new"),
        }
        assert (await env.service.get_watch_changes("jobs")).complete


async def test_single_rpc_partial_probe_survives_restart_and_does_not_starve_reads(tmp_path):
    backend = channels(2)
    settings = Settings(allowed_folder_ids=[1], rpc_pages_per_call=1)
    async with incremental_fixture(tmp_path, backend=backend, settings=settings) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        page = await env.service.get_watch_changes("jobs")
        assert not page.complete and page.batch_id is None
        persisted = env.watches.get("jobs", backend.binding)
        assert persisted is not None
        position = persisted.position
        assert len(position.probes) == 2 and not position.revisions
    async with incremental_fixture(tmp_path, backend=channels(2), settings=settings) as env:
        assert len(await drain(env.service, maximum=4)) == 2
        assert env.backend.peer_requests == []


async def test_change_after_probe_is_seen_by_the_next_sweep(tmp_path, monkeypatch):
    backend = channels(1)
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        original = backend.peer_states

        async def racing(ids):
            states = await original(ids)
            backend.stored_messages.append(msg("-1001", 2))
            return states

        monkeypatch.setattr(backend, "peer_states", racing)
        assert [c.message_ref.message_id for c in await drain(env.service)] == [1]
        monkeypatch.setattr(backend, "peer_states", original)
        assert [c.message_ref.message_id for c in await drain(env.service)] == [2]


@pytest.mark.parametrize("code", ["CONNECTION_ERROR", "RATE_LIMITED"])
async def test_probe_failure_preserves_positions_and_flood_deadline(tmp_path, monkeypatch, code):
    backend = channels(3)
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        original = backend.peer_states
        requests = 0

        async def fail(ids):
            nonlocal requests
            requests += 1
            raise ToolError(code, "Retry later", 30 if code == "RATE_LIMITED" else None)

        monkeypatch.setattr(backend, "peer_states", fail)
        failed = await env.service.get_watch_changes("jobs")
        assert not failed.complete and failed.issues[0].code == code
        assert requests == 1
        persisted = env.watches.get("jobs", backend.binding)
        assert persisted is not None
        assert all(s.position == 0 for s in persisted.position.sources.values())
        if code == "RATE_LIMITED":
            calls = list(backend.backend_calls)
            with pytest.raises(ToolError, match="RATE_LIMITED"):
                await env.service.get_watch_changes("jobs")
            assert backend.backend_calls == calls and requests == 1
            env.clock.now += timedelta(seconds=31)
        monkeypatch.setattr(backend, "peer_states", original)
        assert len(await drain(env.service)) == 3


async def test_failed_peer_batch_falls_back_without_losing_other_sources(tmp_path):
    backend = channels(3)
    backend.faults["-1002"] = ToolError("SOURCE_UNAVAILABLE", "No longer available")
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        page = await env.service.get_watch_changes("jobs")
        assert {c.message_ref.chat_id for c in page.changes} == {"-1001", "-1003"}
        assert not page.complete and page.issue_count == 1


async def test_combined_ack_retry_replays_next_delivery_and_rejects_wrong_batch(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs", limit=1)
        second = await env.service.get_watch_changes("jobs", limit=1, ack_batch_id=first.batch_id)
        assert second.batch_id != first.batch_id
        assert await env.service.get_watch_changes("jobs", ack_batch_id=first.batch_id) == second
        with pytest.raises(ToolError, match="WATCH_BATCH_MISMATCH"):
            await env.service.get_watch_changes("jobs", ack_batch_id="x" * 43)
        assert await env.service.get_watch_changes("jobs") == second


async def test_poll_changes_are_rechecked_even_when_channel_markers_are_identical(tmp_path):
    backend = channels(1)
    backend.stored_messages[0].poll = rich_message(1).poll
    backend.peer_pts["-1001"] = 100
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        await drain(env.service)
        poll = backend.stored_messages[0].poll
        assert poll is not None
        poll.is_closed = not poll.is_closed
        changes = await drain(env.service)
        assert len(changes) == 1 and changes[0].kind == "updated"


async def test_missing_and_regressed_pts_fall_back_without_skipping_an_edit(tmp_path, monkeypatch):
    backend = channels(1)
    backend.peer_pts["-1001"] = 100
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        await drain(env.service)
        backend.peer_pts["-1001"] = 1
        backend.stored_messages[0].text = "Edited after a lower marker"
        assert len(await drain(env.service)) == 1
        original = backend.peer_states

        async def unavailable(ids):
            return {}

        monkeypatch.setattr(backend, "peer_states", unavailable)
        backend.stored_messages[0].text = "Edited without a marker"
        assert len(await drain(env.service)) == 1
        monkeypatch.setattr(backend, "peer_states", original)
        assert not await drain(env.service)


async def test_fresh_protection_and_removed_membership_prevent_batch_content_reads(
    tmp_path, monkeypatch
):
    backend = channels(2)
    async with incremental_fixture(
        tmp_path, backend=backend, settings=Settings(allowed_folder_ids=[1])
    ) as env:
        await env.service.create_watch("jobs", folder_ids=[1], date_from=BASE)
        await drain(env.service)
        backend.folders[0].included_chat_ids = ["-1001"]
        original = backend.peer_states

        async def protected(ids):
            assert ids == ["-1001"]
            states = await original(ids)
            return {cid: replace(state, exportable=False) for cid, state in states.items()}

        monkeypatch.setattr(backend, "peer_states", protected)
        backend.backend_calls.clear()
        page = await env.service.get_watch_changes("jobs")
        assert not page.complete and page.issues[0].code == "EXPORT_PROTECTED"
        assert page.changes == []
        assert not {"recent_observations", "observe_messages", "read_incremental"} & set(
            backend.backend_calls
        )
