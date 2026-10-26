"""Early permitted-source results with bounded buffers and recoverable coverage."""

from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.cursors import CursorStore
from telegram_mcp.application.message_pager import QueryState
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import BASE, FakeBackend, FakeClock, msg


@pytest.mark.parametrize("count", [100, 300])
@pytest.mark.parametrize("order", ["newest_first", "oldest_first"])
async def test_first_page_does_not_prime_every_chat_and_complete_traversal_is_lossless(
    count, order
):
    backend = FakeBackend([msg(str(cid), mid) for cid in range(1, count + 1) for mid in (1, 2, 3)])
    clock = FakeClock()
    cursors = CursorStore(clock)
    service = build_service(
        backend,
        Settings(allowed_chat_ids=[c.id for c in backend.chats]),
        clock=clock,
        cursors=cursors,
    )
    found = []
    cursor = None
    pages = 0
    while True:
        page = await service.search_messages(query="budget", limit=10, order=order, cursor=cursor)
        pages += 1
        assert page.ordering == "page" and page.progress is not None
        assert page.progress.rpc_pages_this_call <= 12
        if pages == 1:
            assert len(page.messages) == 10 and len(backend.fetches) <= 4
            assert page.progress.sources_started < count
        keys = [(m.sent_at, int(m.chat.id), m.message_ref.message_id) for m in page.messages]
        assert keys == sorted(keys, reverse=order == "newest_first")
        found.extend(m.message_ref.key for m in page.messages)
        if page.next_cursor is None:
            assert page.scope.complete and page.progress.sources_started == count
            break
        state = cursors.entries[page.next_cursor].value
        assert isinstance(state, QueryState)
        assert sum(len(s.buffer) for s in state.streams) <= 30
        cursor = page.next_cursor
    assert len(found) == len(set(found)) == count * 3 and not backend.global_fetches
    assert set(found) == {m.message_ref.key for m in backend.stored_messages}
    await service.close()


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"])
async def test_failed_source_does_not_hide_other_results_and_wait_is_respected(code):
    backend = FakeBackend([msg("1", 1), msg("2", 2), msg("3", 3)])
    backend.faults["1"] = ToolError(code, "Retry", 10)
    clock = FakeClock()
    service = build_service(backend, Settings(allowed_chat_ids=["1", "2", "3"]), clock=clock)
    first = await service.search_messages(query="budget")
    assert [m.chat.id for m in first.messages] == ["3", "2"]
    assert first.next_cursor and not first.scope.complete and first.issues[0].code == code
    assert backend.fetches.count("1") == 1
    backend.faults.clear()
    waiting = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert not waiting.messages and backend.fetches.count("1") == 1
    clock.time += 10
    final = await service.search_messages(query="budget", cursor=waiting.next_cursor)
    assert [m.chat.id for m in final.messages] == ["1"]
    assert final.scope.complete and not final.issues
    replay = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert [m.message_ref.key for m in replay.messages] == [
        m.message_ref.key for m in final.messages
    ]
    await service.close()


async def test_round_robin_freezes_scope_and_dates_and_binds_ordering():
    clock = FakeClock(now=BASE + timedelta(minutes=5))
    backend = FakeBackend([msg(cid, mid) for cid in ("1", "2") for mid in range(1, 8)])
    settings = Settings(allowed_chat_ids=["1", "2"])
    service = build_service(backend, settings, clock=clock)
    first = await service.search_messages(query="budget", limit=1)
    assert first.messages[0].chat.id == "1"
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.search_messages(query="budget", cursor=first.next_cursor, ordering="global")
    clock.now += timedelta(days=1)
    backend.stored_messages.append(msg("1", 8))
    backend.chats.append(msg("3", 1).chat)
    settings.allowed_chat_ids.append("3")
    second = await service.search_messages(query="budget", limit=1, cursor=first.next_cursor)
    assert second.messages[0].chat.id == "2"
    settings.allowed_chat_ids.remove("1")
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.search_messages(query="budget", cursor=second.next_cursor)
    settings.allowed_chat_ids.append("1")
    found = first.messages + second.messages
    cursor = second.next_cursor
    while cursor:
        page = await service.search_messages(query="budget", limit=1, cursor=cursor)
        assert page.scope.chat_ids == ["1", "2"]
        found.extend(page.messages)
        cursor = page.next_cursor
    assert {m.message_ref.key for m in found} == {
        f"{cid}:{mid}" for cid in ("1", "2") for mid in range(1, 5)
    }
    await service.close()


async def test_sparse_pages_permanent_failure_and_unicode_clipping_preserve_coverage():
    text = "budget 😀é漢字" * 2000
    backend = FakeBackend([msg("1", 1, text), msg("2", 2), msg("3", 3)])
    backend.faults["2"] = ToolError("NOT_FOUND", "Unavailable")
    service = build_service(
        backend, Settings(allowed_chat_ids=["1", "2", "3"], rpc_pages_per_call=1)
    )
    first = await service.search_messages(query="budget")
    assert first.messages[0].text == text[:3333]
    assert first.messages[0].next_text_offset == 3333
    empty = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert empty.messages == [] and empty.next_cursor and empty.issues[0].chat_id == "2"
    final = await service.search_messages(query="budget", cursor=empty.next_cursor)
    assert final.messages[0].chat.id == "3" and final.next_cursor is None
    assert not final.scope.complete and final.issues == empty.issues
    assert first.progress is not None and final.progress is not None
    assert first.progress.sources_started == 1 and final.progress.sources_started == 2
    await service.close()
