"""Search planning, fair priming, access checks and recoverable global cursors."""

from datetime import timedelta

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.application.ports import GlobalBatch, GlobalOffset
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Folder, ToolError
from tests.support.fakes import BASE, FakeBackend, FakeClock, msg


async def test_unrestricted_search_reads_one_global_page_for_300_sources() -> None:
    backend = FakeBackend([msg(str(i), 1) for i in range(1, 301)])
    service = build_service(backend, Settings(allow_all_chats=True))
    page = await service.search_messages(query="budget", limit=30)
    assert len(page.messages) == 30
    assert [int(m.chat.id) for m in page.messages] == list(range(300, 270, -1))
    assert len(backend.global_fetches) == 1
    assert backend.fetches == []
    assert page.next_cursor and not page.scope.complete


@pytest.mark.parametrize("selector", ["policy", "chats", "folders", "sender", "oldest"])
async def test_restricted_or_specialized_search_never_calls_global(selector: str) -> None:
    backend = FakeBackend(
        [msg("1", 1), msg("2", 2)],
        folders=[Folder(id=1, title="Selected", position=0, included_chat_ids=["1"])],
    )
    service = build_service(
        backend,
        Settings(
            allow_all_chats=selector != "policy",
            allowed_chat_ids=["1"],
        ),
    )
    if selector == "chats":
        await service.search_messages(query="budget", chat_ids=["1"])
    elif selector == "folders":
        await service.search_messages(query="budget", folder_ids=[1])
    elif selector == "sender":
        await service.search_messages(query="budget", sender_id="42")
    elif selector == "oldest":
        await service.search_messages(query="budget", order="oldest_first")
    else:
        await service.search_messages(query="budget")
    assert backend.global_fetches == []
    if selector in {"policy", "chats", "folders"}:
        assert backend.fetches == ["1"]


async def test_restricted_300_source_search_reports_progress_and_strict_order() -> None:
    backend = FakeBackend([msg(str(i), 1) for i in range(1, 301)])
    service = build_service(backend, Settings(allowed_chat_ids=[c.id for c in backend.chats]))
    page = await service.search_messages(ordering="global", query="budget", limit=100)
    assert not page.messages and page.progress
    assert page.progress.sources_total == 300
    assert page.progress.sources_ready == 12
    for call_number in range(2, 26):
        assert page.next_cursor
        page = await service.search_messages(
            ordering="global", query="budget", limit=100, cursor=page.next_cursor
        )
        if call_number < 25:
            assert not page.messages
            assert page.progress and page.progress.sources_ready == 12 * call_number
    found = list(page.messages)
    while page.next_cursor:
        page = await service.search_messages(
            ordering="global", query="budget", limit=100, cursor=page.next_cursor
        )
        found.extend(page.messages)
    assert [int(m.chat.id) for m in found] == list(range(300, 0, -1))
    assert len(backend.fetches) == 300 and not backend.global_fetches
    assert page.scope.complete


async def test_sparse_source_cannot_starve_other_sources_during_priming() -> None:
    backend = FakeBackend(
        [msg("1", i, content="voice" if i == 1 else "text") for i in range(1, 91)]
        + [msg("2", 1, content="voice"), msg("3", 1, content="voice")]
    )
    service = build_service(
        backend, Settings(allowed_chat_ids=["1", "2", "3"], rpc_pages_per_call=1)
    )
    page = await service.search_messages(ordering="global", query="budget", content_type="voice")
    assert backend.fetches == ["1"]
    for _ in range(2):
        page = await service.search_messages(
            ordering="global", query="budget", content_type="voice", cursor=page.next_cursor
        )
    assert backend.fetches == ["1", "2", "3"]
    assert page.progress and page.progress.sources_ready == 2
    assert not page.messages
    while page.next_cursor:
        page = await service.search_messages(
            ordering="global", query="budget", content_type="voice", cursor=page.next_cursor
        )
    assert [m.chat.id for m in page.messages] == ["3", "2", "1"]


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"])
async def test_global_search_resumes_same_position_after_retry_deadline(code: str) -> None:
    clock = FakeClock()
    backend = FakeBackend([msg("1", i) for i in range(1, 61)])
    service = build_service(backend, Settings(allow_all_chats=True), clock=clock)
    first = await service.search_messages(query="budget")
    assert len(first.messages) == 30 and first.next_cursor
    backend.global_fault = ToolError(code, "Try later", 10)
    failed = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert not failed.messages and failed.next_cursor and not failed.scope.complete
    assert failed.issues[0].retry_after_seconds == 10
    backend.global_fault = None
    waiting = await service.search_messages(query="budget", cursor=failed.next_cursor)
    assert not waiting.messages and len(backend.global_fetches) == 2
    clock.time += 10
    resumed = await service.search_messages(query="budget", cursor=waiting.next_cursor)
    assert [m.message_ref.message_id for m in first.messages + resumed.messages] == list(
        range(60, 0, -1)
    )
    assert resumed.scope.complete and not resumed.issues
    assert backend.global_fetches[1] == backend.global_fetches[2]


async def test_global_buffer_is_hidden_when_authorization_or_policy_check_fails() -> None:
    backend = FakeBackend([msg("1", 1), msg("1", 2)])
    settings = Settings(allow_all_chats=True, allowed_chat_ids=["1"])
    service = build_service(backend, settings)
    first = await service.search_messages(query="budget", limit=1)
    backend.authorization_fault = ToolError("CONNECTION_ERROR", "Cannot check authorization")
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await service.search_messages(query="budget", cursor=first.next_cursor)
    backend.authorization_fault = None
    settings.allow_all_chats = False
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.search_messages(query="budget", cursor=first.next_cursor)
    settings.allow_all_chats = True
    resumed = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert [m.message_ref.message_id for m in resumed.messages] == [1]
    assert len(backend.global_fetches) == 1


async def test_per_chat_cursor_does_not_switch_engine_when_policy_expands() -> None:
    backend = FakeBackend([msg("1", 1), msg("1", 2)])
    settings = Settings(allowed_chat_ids=["1"])
    service = build_service(backend, settings)
    first = await service.search_messages(query="budget", limit=1)
    settings.allow_all_chats = True
    resumed = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert [m.message_ref.message_id for m in resumed.messages] == [1]
    assert not backend.global_fetches


async def test_global_scope_and_date_ceiling_stay_frozen_across_pages() -> None:
    clock = FakeClock(now=BASE + timedelta(minutes=40))
    backend = FakeBackend([msg("1", i) for i in range(1, 66)])
    service = build_service(
        backend, Settings(allow_all_chats=True, rpc_pages_per_call=1), clock=clock
    )
    page = await service.search_messages(
        query="budget", date_from=BASE + timedelta(minutes=10), limit=7
    )
    found = list(page.messages)
    clock.now += timedelta(days=1)
    backend.chats.append(msg("2", 1).chat)
    backend.stored_messages.extend([msg("2", 15), msg("1", 70)])
    while page.next_cursor:
        page = await service.search_messages(
            query="budget", date_from=BASE + timedelta(minutes=10), limit=7, cursor=page.next_cursor
        )
        assert page.scope.chat_ids == ["1"]
        found.extend(page.messages)
    assert [m.message_ref.message_id for m in found] == list(range(39, 9, -1))


async def test_global_nonadvancing_page_is_explicitly_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend([msg("1", 1)])

    async def stalled(*args: object, **kwargs: object) -> GlobalBatch:
        return GlobalBatch([], GlobalOffset(), False)

    monkeypatch.setattr(backend, "search_global", stalled)
    page = await build_service(backend, Settings(allow_all_chats=True)).search_messages(
        query="budget"
    )
    assert not page.scope.complete and page.next_cursor is None
    assert page.issues[0].code == "PAGINATION_STALLED"


async def test_empty_query_requires_positive_link_filter_and_binds_cursor() -> None:
    linked = msg("1", 2).model_copy(update={"has_link": True})
    backend = FakeBackend(
        [msg("1", 1), linked, linked.model_copy(update={"message_ref": msg("1", 3).message_ref})]
    )
    service = build_service(backend, Settings(allow_all_chats=True))
    for has_link in (None, False):
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await service.search_messages(query="", has_link=has_link)
    first = await service.search_messages(query="", has_link=True, limit=1)
    assert first.messages[0].has_link and first.next_cursor
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.search_messages(query="budget", has_link=False, cursor=first.next_cursor)


async def test_folder_dependent_cursor_remains_usable_after_catalog_loading_failure() -> None:
    backend = FakeBackend(
        [msg("1", 1), msg("1", 2)],
        folders=[
            Folder(id=1, title="Allowed", position=0, included_chat_ids=["1"]),
        ],
    )
    service = build_service(backend, Settings(allowed_folder_ids=[1]))
    first = await service.get_history(chat_id="1", limit=1)
    backend.catalog_fault = ToolError("CATALOG_LOADING", "Loaded 100 dialogs", 1)
    with pytest.raises(ToolError, match="CATALOG_LOADING"):
        await service.get_history(chat_id="1", cursor=first.next_cursor)
    backend.catalog_fault = None
    final = await service.get_history(chat_id="1", cursor=first.next_cursor)
    assert [m.message_ref.message_id for m in final.messages] == [1]
    assert len(backend.fetches) == 1


@pytest.mark.parametrize("tool", ["history", "messages", "chat", "context"])
async def test_direct_reads_skip_catalog_but_check_authorization(tool: str) -> None:
    backend = FakeBackend([msg("1", 1)])
    backend.catalog_fault = ToolError("CATALOG_LOADING", "Unrelated catalog is incomplete", 1)
    service = build_service(backend, Settings(allowed_chat_ids=["1"]))
    if tool == "history":
        await service.get_history(chat_id="1")
    elif tool == "messages":
        await service.get_messages(["1:1"])
    elif tool == "chat":
        await service.get_chat("1")
    else:
        await service.get_context("1:1")
    assert "catalog" not in backend.backend_calls
    assert backend.backend_calls.count("authorization") == 1


@pytest.mark.parametrize("overlap", [True, False])
async def test_global_pages_deduplicate_overlap_and_reject_changed_order(
    overlap: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([msg("1", i) for i in range(1, 5)])
    calls = 0

    async def pages(*args: object, **kwargs: object) -> GlobalBatch:
        nonlocal calls
        calls += 1
        if calls == 1:
            return GlobalBatch([msg("1", 3), msg("1", 2)], GlobalOffset(2, "1", 2), False)
        rows = [msg("1", 2), msg("1", 1)] if overlap else [msg("1", 4), msg("1", 1)]
        return GlobalBatch(rows, GlobalOffset(1, "1", 1), True)

    monkeypatch.setattr(backend, "search_global", pages)
    service = build_service(backend, Settings(allow_all_chats=True))
    first = await service.search_messages(query="budget", limit=2)
    final = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert [m.message_ref.message_id for m in first.messages] == [3, 2]
    if overlap:
        assert [m.message_ref.message_id for m in final.messages] == [1]
        assert final.scope.complete
    else:
        assert not final.messages and not final.scope.complete
        assert final.issues[0].code == "PAGINATION_INCOMPLETE"


async def test_global_same_second_rows_keep_native_order_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend([msg("9", 100, minute=1), msg("10", 99, minute=1)])
    calls = 0

    async def pages(*args: object, **kwargs: object) -> GlobalBatch:
        nonlocal calls
        calls += 1
        message = backend.stored_messages[calls - 1]
        return GlobalBatch(
            [message],
            GlobalOffset(calls, message.chat.id, message.message_ref.message_id),
            calls == 2,
        )

    monkeypatch.setattr(backend, "search_global", pages)
    service = build_service(backend, Settings(allow_all_chats=True))
    first = await service.search_messages(query="budget", limit=1)
    final = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert [m.message_ref.key for m in first.messages + final.messages] == ["9:100", "10:99"]
    assert final.scope.complete
