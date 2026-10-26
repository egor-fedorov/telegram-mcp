"""Bounded multi-source history: early pages, cumulative budgets and fixed selections."""

from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.cursors import CursorStore
from telegram_mcp.application.message_pager import QueryState
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat, Folder, ToolError
from tests.support.fakes import BASE, FakeBackend, FakeClock, msg

FROM, TO = BASE, BASE + timedelta(days=7)


def folder_backend(count: int, per_chat: int = 3) -> tuple[FakeBackend, list[str]]:
    ids = [str(-1001000000000 - index) for index in range(count)]
    chats = [Chat(id=cid, title=f"Jobs {index}", type="channel") for index, cid in enumerate(ids)]
    messages = [
        msg(cid, mid, "vacancy", minute=index * per_chat + mid)
        for index, cid in enumerate(ids)
        for mid in range(1, per_chat + 1)
    ]
    for message, chat in zip(messages, [c for c in chats for _ in range(per_chat)], strict=True):
        message.chat = chat
    folder = Folder(id=7, title="Jobs", position=0, included_chat_ids=ids)
    return FakeBackend(messages, chats=chats, folders=[folder]), ids


def service_for(backend: FakeBackend, settings: Settings, clock: FakeClock | None = None):
    clock = clock or FakeClock(now=TO + timedelta(days=1))
    cursors = CursorStore(clock)
    return build_service(backend, settings, clock=clock, cursors=cursors), cursors, clock


async def test_default_source_budget_rejects_large_folder_before_history_reads():
    backend, _ = folder_backend(100)
    service, _, _ = service_for(backend, Settings(allowed_folder_ids=[7]))
    with pytest.raises(ToolError) as caught:
        await service.get_history(folder_ids=[7], date_from=FROM, date_to=TO)
    assert caught.value.code == "SOURCE_LIMIT_EXCEEDED"
    assert "100 sources" in caught.value.detail and "max_sources is 20" in caught.value.detail
    assert backend.fetches == []
    await service.close()


async def test_explicit_chat_ids_over_budget_fail_before_resolving_sources():
    backend, ids = folder_backend(30)
    service, _, _ = service_for(backend, Settings(allowed_chat_ids=ids))
    with pytest.raises(ToolError, match="SOURCE_LIMIT_EXCEEDED"):
        await service.get_history(chat_ids=ids, date_from=FROM, date_to=TO, max_sources=29)
    assert backend.fetches == [] and backend.resolve_calls == []
    await service.close()


@pytest.mark.parametrize("order", ["newest_first", "oldest_first"])
async def test_hundred_channel_folder_returns_early_and_traverses_losslessly(order):
    backend, ids = folder_backend(100)
    service, cursors, _ = service_for(backend, Settings(allowed_folder_ids=[7]))
    found: list[str] = []
    cursor = None
    calls = 0
    while True:
        page = await service.get_history(
            folder_ids=[7],
            date_from=FROM,
            date_to=TO,
            order=order,
            limit=50,
            cursor=cursor,
            max_sources=100,
            max_messages=1000,
        )
        calls += 1
        assert page.ordering == "page" and page.progress is not None and page.budget is not None
        assert page.progress.rpc_pages_this_call <= 12
        if calls == 1:
            # The first page is bounded by the per-call RPC budget, not by 100 heads.
            assert page.messages and len(backend.fetches) <= 12
            assert page.progress.sources_started < 100
        keys = [(m.sent_at, int(m.chat.id), m.message_ref.message_id) for m in page.messages]
        assert keys == sorted(keys, reverse=order == "newest_first")
        assert all(FROM <= m.sent_at < TO for m in page.messages)
        found.extend(m.message_ref.key for m in page.messages)
        if page.next_cursor is None:
            assert page.scope.complete and not page.budget.exhausted
            assert page.budget.emitted_messages == 300
            break
        assert isinstance(cursors.entries[page.next_cursor].value, QueryState)
        cursor = page.next_cursor
    assert len(found) == len(set(found)) == 300
    assert set(found) == {m.message_ref.key for m in backend.stored_messages}
    assert set(backend.fetches) == set(ids)
    await service.close()


async def test_message_budget_is_cumulative_replay_safe_and_explicitly_raisable():
    backend, ids = folder_backend(4, per_chat=10)
    service, _, _ = service_for(backend, Settings(allowed_chat_ids=ids))
    args = {"chat_ids": ids, "date_from": FROM, "date_to": TO, "limit": 10, "max_messages": 25}
    first = await service.get_history(**args)
    second = await service.get_history(**args, cursor=first.next_cursor)
    third = await service.get_history(**args, cursor=second.next_cursor)
    assert [len(p.messages) for p in (first, second, third)] == [10, 10, 5]
    assert third.budget is not None and third.budget.exhausted
    assert third.budget.emitted_messages == 25 and third.next_cursor is not None
    assert not third.scope.complete
    fetched = len(backend.fetches)
    capped = await service.get_history(**args, cursor=third.next_cursor)
    assert capped.messages == [] and capped.budget is not None and capped.budget.exhausted
    assert len(backend.fetches) == fetched and capped.next_cursor is not None
    replay = await service.get_history(**args, cursor=second.next_cursor)
    assert [m.message_ref.key for m in replay.messages] == [
        m.message_ref.key for m in third.messages
    ]
    assert replay.budget is not None and replay.budget.emitted_messages == 25
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await service.get_history(**{**args, "max_messages": 20}, cursor=capped.next_cursor)
    keys = {m.message_ref.key for p in (first, second, third) for m in p.messages}
    cursor = capped.next_cursor
    while cursor is not None:
        page = await service.get_history(**{**args, "max_messages": 40}, cursor=cursor)
        keys.update(m.message_ref.key for m in page.messages)
        cursor = page.next_cursor
    assert page.budget is not None and page.budget.emitted_messages == 40
    assert not page.budget.exhausted and page.scope.complete and len(keys) == 40
    await service.close()


async def test_selection_and_upper_date_are_frozen_and_duplicates_read_once():
    backend, ids = folder_backend(3, per_chat=2)
    clock = FakeClock(now=BASE + timedelta(minutes=30))
    service, _, _ = service_for(backend, Settings(allowed_folder_ids=[7]), clock)
    first = await service.get_history(
        folder_ids=[7, 7], date_from=FROM, date_to=TO, limit=2, max_messages=1000
    )
    assert first.scope.chat_ids == ids and first.next_cursor
    late = msg(ids[0], 99, "vacancy")
    late.chat = backend.chats[0]
    late.sent_at = clock.now + timedelta(minutes=1)
    backend.stored_messages.append(late)
    newcomer = Chat(id="-1009", title="New", type="channel")
    backend.chats.append(newcomer)
    backend.folders[0].included_chat_ids.append(newcomer.id)
    cursor, keys = first.next_cursor, {m.message_ref.key for m in first.messages}
    while cursor:
        page = await service.get_history(
            folder_ids=[7, 7], date_from=FROM, date_to=TO, limit=2, cursor=cursor
        )
        keys.update(m.message_ref.key for m in page.messages)
        cursor = page.next_cursor
    assert late.message_ref.key not in keys and len(keys) == 6
    assert "-1009" not in backend.fetches
    await service.close()


async def test_folder_revocation_blocks_buffered_continuation():
    backend, ids = folder_backend(3, per_chat=5)
    settings = Settings(allowed_folder_ids=[7])
    service, _, _ = service_for(backend, settings)
    first = await service.get_history(folder_ids=[7], date_from=FROM, date_to=TO, limit=2)
    assert first.next_cursor
    backend.folders[0].included_chat_ids.remove(ids[0])
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_history(
            folder_ids=[7], date_from=FROM, date_to=TO, limit=2, cursor=first.next_cursor
        )
    await service.close()


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"])
async def test_transient_source_failure_keeps_other_results_and_cursor(code):
    backend, ids = folder_backend(3, per_chat=2)
    backend.faults[ids[1]] = ToolError(code, "Retry", 10)
    service, _, clock = service_for(backend, Settings(allowed_chat_ids=ids))
    first = await service.get_history(chat_ids=ids, date_from=FROM, date_to=TO)
    assert {m.chat.id for m in first.messages} == {ids[0], ids[2]}
    assert first.issues[0].code == code and first.next_cursor and not first.scope.complete
    backend.faults.clear()
    clock.time += 10
    final = await service.get_history(
        chat_ids=ids, date_from=FROM, date_to=TO, cursor=first.next_cursor
    )
    assert {m.chat.id for m in final.messages} == {ids[1]} and final.scope.complete
    await service.close()


async def test_global_ordering_and_text_continuation_remain_available():
    backend, ids = folder_backend(3, per_chat=2)
    backend.stored_messages[0].text = "x" * 15000
    service, _, _ = service_for(backend, Settings(allowed_chat_ids=ids))
    page = await service.get_history(
        chat_ids=ids, date_from=FROM, date_to=TO, ordering="global", limit=6
    )
    assert page.ordering == "global" and len(page.messages) == 6
    keys = [(m.sent_at, int(m.chat.id), m.message_ref.message_id) for m in page.messages]
    assert keys == sorted(keys, reverse=True)
    long = next(m for m in page.messages if m.text_total_length == 15000)
    assert long.next_text_offset == len(long.text) < 15000
    await service.close()


async def test_public_channel_exception_applies_only_to_explicit_chat_ids():
    public = Chat(id="-1005", title="Public", type="channel", username="public_jobs")
    backend = FakeBackend([msg("-1005", 1)], chats=[], external_chats=[public])
    backend.stored_messages[0].chat = public
    service, _, _ = service_for(backend, Settings(allow_public_channel_reads=True))
    page = await service.get_history(chat_ids=["-1005"], date_from=FROM, date_to=TO)
    assert [m.message_ref.key for m in page.messages] == ["-1005:1"]
    assert backend.public_checks == ["-1005"]
    await service.close()


async def test_legacy_single_source_calls_have_no_implicit_budget():
    backend = FakeBackend([msg("1", mid) for mid in range(1, 4)])
    service, _, _ = service_for(backend, Settings(allowed_chat_ids=["1"]))
    page = await service.get_history(chat_id="1")
    assert page.ordering == "global" and page.budget is None and page.progress is None
    bounded = await service.get_history(chat_id="1", max_messages=2, limit=30)
    assert len(bounded.messages) == 2 and bounded.budget is not None
    assert bounded.budget.exhausted and bounded.next_cursor
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_history(chat_id="1", cursor=bounded.next_cursor)
    await service.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"folder_ids": [7]},
        {"folder_ids": [7], "date_from": FROM},
        {"chat_ids": ["1"], "date_from": FROM, "date_to": TO, "topic_id": 1},
        {"chat_id": "1", "chat_ids": ["1"], "date_from": FROM, "date_to": TO},
        {"chat_ids": [], "date_from": FROM, "date_to": TO},
        {"chat_ids": ["1"], "date_from": FROM, "date_to": TO, "max_messages": 0},
        {"chat_ids": ["1"], "date_from": FROM, "date_to": TO, "max_sources": 1001},
    ],
)
async def test_invalid_multi_source_arguments_are_rejected_before_reads(kwargs):
    backend = FakeBackend([msg("1", 1)])
    service, _, _ = service_for(backend, Settings(allowed_chat_ids=["1"]))
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await service.get_history(**kwargs)
    assert backend.fetches == []
    await service.close()
