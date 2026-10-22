from datetime import timedelta
from typing import Literal

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat, Folder, MessageRef, Page, ToolError
from tests.fakes import BASE, FakeBackend, FakeClock, msg


async def test_global_pagination_no_losses_duplicates_or_reordering() -> None:
    messages = [
        msg(cid, i, minute=2 * i + extra)
        for cid, extra in (("1", 0), ("2", 1))
        for i in range(1, 66)
    ]
    service = build_service(FakeBackend(messages), Settings(allow_all_chats=True))
    page = await service.search_messages(query="budget", limit=7)
    found = list(page.messages)
    first = page
    while page.next_cursor:
        page = await service.search_messages(query="budget", limit=7, cursor=page.next_cursor)
        found.extend(page.messages)
    assert len(found) == 130
    assert len({m.message_ref.key for m in found}) == 130
    assert [m.sent_at for m in found] == sorted((m.sent_at for m in messages), reverse=True)
    assert page.scope.complete
    replay = await service.search_messages(query="budget", limit=7, cursor=first.next_cursor)
    assert [m.message_ref.key for m in replay.messages] == [m.message_ref.key for m in found[7:14]]


async def test_empty_pages_while_priming_are_resumable() -> None:
    service = build_service(
        FakeBackend([msg(str(i), i) for i in range(1, 5)]),
        Settings(allowed_chat_ids=[str(i) for i in range(1, 5)], rpc_pages_per_call=1),
    )
    page = await service.search_messages(query="budget")
    assert page.messages == []
    assert page.next_cursor and not page.scope.complete
    for _ in range(3):
        page = await service.search_messages(query="budget", cursor=page.next_cursor)
    assert [m.message_ref.message_id for m in page.messages] == [4, 3, 2, 1]
    assert page.scope.complete


async def test_content_filter_advances_even_when_page_has_no_matches() -> None:
    service = build_service(
        FakeBackend([msg("1", i, content="voice" if i == 1 else "text") for i in range(1, 65)]),
        Settings(allow_all_chats=True, rpc_pages_per_call=1),
    )
    page = await service.search_messages(query="budget", content_type="voice")
    assert not page.messages and page.next_cursor
    while page.next_cursor:
        page = await service.search_messages(
            query="budget", content_type="voice", cursor=page.next_cursor
        )
    assert [m.message_ref.message_id for m in page.messages] == [1]


async def test_dates_and_oldest_first_history() -> None:
    service = build_service(
        FakeBackend([msg("1", i) for i in range(1, 8)]), Settings(allow_all_chats=True)
    )
    page = await service.get_history(
        chat_id="1",
        date_from=BASE + timedelta(minutes=2),
        date_to=BASE + timedelta(minutes=5),
        order="oldest_first",
    )
    assert [m.message_ref.message_id for m in page.messages] == [2, 3, 4]
    with pytest.raises(ToolError, match="timezone"):
        await service.search_messages(query="budget", date_from=BASE.replace(tzinfo=None))
    with pytest.raises(ToolError, match="date_from"):
        await service.search_messages(query="budget", date_from=BASE, date_to=BASE)


async def test_cursor_query_binding() -> None:
    service = build_service(
        FakeBackend([msg("1", i) for i in range(1, 5)]), Settings(allow_all_chats=True)
    )
    page = await service.search_messages(query="budget", limit=1)
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.search_messages(query="other", cursor=page.next_cursor)
    with pytest.raises(ToolError, match="CURSOR_EXPIRED"):
        await service.search_messages(query="budget", cursor="invented")


async def test_folder_snapshot_and_permission_revocation() -> None:
    folder = Folder(id=1, title="Work", position=0, included_chat_ids=["1", "2"])
    backend = FakeBackend([msg("1", 1), msg("2", 2)], folders=[folder])
    service = build_service(backend, Settings(allow_all_chats=True))
    page = await service.list_chats(folder_ids=[1], limit=1)
    folder.included_chat_ids = ["2"]
    continued = await service.list_chats(folder_ids=[1], limit=1, cursor=page.next_cursor)
    assert continued.scope.chat_ids == ["1", "2"]
    fresh = await service.list_chats(folder_ids=[1])
    assert fresh.scope.chat_ids == ["2"]
    folder.included_chat_ids = ["1", "2"]
    restricted = build_service(backend, Settings(allowed_folder_ids=[1]))
    first = await restricted.search_messages(query="budget", limit=1)
    folder.included_chat_ids = ["2"]
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await restricted.search_messages(query="budget", cursor=first.next_cursor)


async def test_permanent_failures_are_explicit_and_other_sources_continue() -> None:
    backend = FakeBackend([msg("1", 1), msg("2", 2)])
    backend.faults["2"] = ToolError("ACCESS_DENIED", "Unavailable source")
    page = await build_service(backend, Settings(allowed_chat_ids=["1", "2"])).search_messages(
        query="budget"
    )
    assert [m.chat.id for m in page.messages] == ["1"]
    assert not page.scope.complete
    assert page.next_cursor is None
    assert page.issues[0].code == "ACCESS_DENIED"
    assert page.issues[0].chat_id == "2"


async def test_context_is_chronological_and_blocks_cross_chat_reads() -> None:
    parent = MessageRef(chat_id="2", message_id=1)
    backend = FakeBackend(
        [msg("1", i, reply_to=parent if i == 3 else None) for i in range(1, 6)] + [msg("2", 1)]
    )
    service = build_service(backend, Settings(allowed_chat_ids=["1"]))
    context = await service.get_context("1:3", before=2, after=2)
    assert [m.message_ref.message_id for m in context.before] == [1, 2]
    assert [m.message_ref.message_id for m in context.after] == [4, 5]
    assert context.issues[0].code == "ACCESS_DENIED"
    assert "2:1" not in backend.reads
    result = await service.get_messages([parent])
    assert not result.messages and result.issues[0].code == "ACCESS_DENIED"


async def test_missing_messages_and_complete_text_retrieval() -> None:
    backend = FakeBackend([msg("1", 1, text="a" * 15000)])
    service = build_service(backend, Settings(allow_all_chats=True))
    first = await service.get_messages(["1:1", "1:999"])
    assert first.messages[0].next_text_offset == 12000
    assert first.issues[0].code == "MESSAGE_UNAVAILABLE"
    tail = await service.get_messages(["1:1"], text_offset=12000)
    assert first.messages[0].text + tail.messages[0].text == "a" * 15000
    assert tail.messages[0].next_text_offset is None


async def test_linked_discussion_is_checked_before_loading_its_messages() -> None:
    channel = Chat(id="-1001", title="News", type="channel")
    group = Chat(id="-1002", title="Discussion", type="group")
    backend = FakeBackend(chats=[channel, group])
    service = build_service(backend, Settings(allowed_chat_ids=["-1001"]))
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_thread("-1001:1")
    assert backend.thread_calls == 0
    assert not backend.fetches


async def test_channel_thread_uses_the_discussion_root() -> None:
    root = MessageRef(chat_id="-1002", message_id=10)
    backend = FakeBackend(
        [msg("-1002", 11, reply_to=root)],
        chats=[
            Chat(id="-1001", title="News", type="channel"),
            Chat(id="-1002", title="Discussion", type="group"),
        ],
    )
    page = await build_service(backend, Settings(allow_all_chats=True)).get_thread("-1001:1")
    assert page.thread_root == root
    assert page.messages[0].message_ref.message_id == 11


@pytest.mark.parametrize("reference", ["1", "@history", "https://t.me/history"])
async def test_history_accepts_and_refreshes_explicit_reference(reference: str) -> None:
    backend = FakeBackend(
        [msg("1", 1)], chats=[Chat(id="1", title="History", type="channel", username="history")]
    )
    service = build_service(backend, Settings(allowed_chat_ids=["1"]))
    page = await service.get_history(chat_ref=reference)
    assert [m.message_ref.key for m in page.messages] == ["1:1"]
    assert (reference, True) in backend.resolve_calls


async def test_history_requires_exactly_one_source() -> None:
    backend = FakeBackend([msg("1", 1)])
    service = build_service(backend, Settings(allow_all_chats=True))
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await service.get_history()
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await service.get_history(chat_id="1", chat_ref="1")
    assert not backend.fetches
    page = await service.get_history(chat_id="1")
    assert page.messages[0].message_ref.key == "1:1"


async def test_unknown_numeric_reference_does_not_trigger_catalog_scan() -> None:
    backend = FakeBackend([msg("1", 1)], chats=[])
    service = build_service(backend, Settings(allowed_chat_ids=["1"]))
    with pytest.raises(ToolError, match="NOT_FOUND"):
        await service.get_history(chat_ref="1")
    assert "catalog" not in backend.backend_calls
    assert not backend.fetches


async def test_history_cursor_binds_reference_and_resolved_identity() -> None:
    channel = Chat(id="-1001", title="News", type="channel", username="news")
    backend = FakeBackend([msg("-1001", 1), msg("-1001", 2), msg("-1002", 3)], chats=[channel])
    service = build_service(backend, Settings(allow_all_chats=True))
    first = await service.get_history(chat_ref="@news", limit=1)
    assert first.next_cursor
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_history(chat_ref="-1001", limit=1, cursor=first.next_cursor)
    channel.username = "old_news"
    backend.chats.append(Chat(id="-1002", title="New owner", type="channel", username="news"))
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_history(chat_ref="@news", limit=1, cursor=first.next_cursor)
    assert backend.fetches == ["-1001"]


def public_backend() -> tuple[FakeBackend, Chat]:
    public = Chat(id="-1009", title="Public news", type="channel", username="public_news")
    backend = FakeBackend(
        [msg("1", 1)] + [msg(public.id, mid) for mid in range(1, 4)],
        chats=[Chat(id="1", title="Allowed", type="group")],
        external_chats=[public],
    )
    return backend, public


async def test_public_reads_are_disabled_by_default() -> None:
    backend, public = public_backend()
    settings = Settings(allowed_chat_ids=["1"])
    service = build_service(backend, settings)
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_history(chat_ref="@public_news")
    assert not backend.fetches
    assert not backend.public_checks
    settings.allowed_chat_ids.append(public.id)
    page = await service.get_history(chat_ref="@public_news")
    assert page.messages and page.scope.chat_ids == [public.id]


async def test_public_reads_require_explicit_scope_and_never_expand_catalog_or_folders() -> None:
    backend, public = public_backend()
    folder = Folder(id=1, title="Channels", position=0, include_channels=True)
    backend.folders = [folder]
    backend.chats.append(Chat(id="-1008", title="Other public", type="channel", username="other"))
    backend.stored_messages.append(msg("-1008", 4))
    service = build_service(
        backend, Settings(allowed_chat_ids=["1"], allow_public_channel_reads=True)
    )
    explicit = await service.get_history(chat_ref="@public_news")
    assert [m.message_ref.message_id for m in explicit.messages] == [3, 2, 1]
    assert explicit.scope.chat_ids == [public.id]
    assert public.id in backend.public_checks
    listed = await service.list_chats()
    implicit = await service.search_messages(query="budget")
    from_folder = await service.search_messages(query="budget", folder_ids=[1])
    assert [c.id for c in listed.chats] == ["1"]
    assert implicit.scope.chat_ids == ["1"]
    assert from_folder.scope.chat_ids == []
    assert not from_folder.messages
    assert public.id not in {chat.id for chat in backend.chats}


@pytest.mark.parametrize("revocation", ["private", "group", "disabled", "denied"])
async def test_public_grant_is_revalidated_before_buffered_cursor_output(revocation: str) -> None:
    backend, public = public_backend()
    settings = Settings(allow_public_channel_reads=True)
    service = build_service(backend, settings)
    first = await service.get_history(chat_id=public.id, limit=1)
    assert first.next_cursor
    if revocation == "private":
        public.username = None
    elif revocation == "group":
        public.type = "group"
    elif revocation == "disabled":
        settings.allow_public_channel_reads = False
    else:
        backend.public_faults[public.id] = ToolError("ACCESS_DENIED", "No access")
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_history(chat_id=public.id, limit=1, cursor=first.next_cursor)
    assert backend.fetches == [public.id]


@pytest.mark.parametrize("source", ["public", "authorization"])
async def test_metadata_failure_hides_buffer_and_preserves_original_cursor(source: str) -> None:
    backend, public = public_backend()
    service = build_service(backend, Settings(allow_public_channel_reads=True))
    first = await service.get_history(chat_id=public.id, limit=1)
    error = ToolError("CONNECTION_ERROR", "Metadata unavailable")
    if source == "public":
        backend.public_faults[public.id] = error
    else:
        backend.authorization_fault = error
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await service.get_history(chat_id=public.id, limit=1, cursor=first.next_cursor)
    backend.public_faults.clear()
    backend.authorization_fault = None
    resumed = await service.get_history(chat_id=public.id, limit=1, cursor=first.next_cursor)
    assert [m.message_ref.message_id for m in resumed.messages] == [2]
    assert not resumed.issues
    assert backend.fetches == [public.id]


async def test_public_grant_does_not_authorize_reply_parent_or_discussion() -> None:
    backend, public = public_backend()
    parent = MessageRef(chat_id="-1008", message_id=1)
    backend.external_chats.extend(
        [
            Chat(id=parent.chat_id, title="Parent", type="channel", username="parent"),
            Chat(id="-1002", title="Discussion", type="group", is_supergroup=True),
        ]
    )
    backend.stored_messages = [msg(public.id, 1, reply_to=parent), msg(parent.chat_id, 1)]
    service = build_service(backend, Settings(allow_public_channel_reads=True))
    result = await service.get_context(f"{public.id}:1", before=0, after=0)
    assert result.anchor and result.anchor.message_ref.chat_id == public.id
    assert result.reply_chain == []
    assert result.issues[0].code == "ACCESS_DENIED"
    assert parent.key not in backend.reads
    assert parent.chat_id not in backend.public_checks
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_thread(f"{public.id}:1")
    assert backend.thread_calls == 0


async def test_batched_messages_preserve_input_order_duplicates_and_indexed_issues() -> None:
    backend = FakeBackend([msg("1", 1), msg("1", 2), msg("2", 3), msg("3", 4)])
    service = build_service(backend, Settings(allowed_chat_ids=["1", "2"]))
    refs: list[str | MessageRef] = ["2:3", "1:2", "1:999", "1:2", "invalid", "3:4", "1:1"]
    result = await service.get_messages(refs)
    assert [m.message_ref.key for m in result.messages] == ["2:3", "1:2", "1:2", "1:1"]
    assert [(issue.input_index, issue.code) for issue in result.issues] == [
        (2, "MESSAGE_UNAVAILABLE"),
        (4, "INVALID_ARGUMENT"),
        (5, "ACCESS_DENIED"),
    ]
    assert len(backend.batch_reads) == 2
    assert {cid: mids for cid, mids in backend.batch_reads} == {"2": [3], "1": [2, 999, 1]}
    assert not backend.reads


async def test_batched_message_failure_affects_each_input_without_hiding_other_chat() -> None:
    backend = FakeBackend([msg("1", 1), msg("2", 2)])
    backend.message_faults["1"] = ToolError("RATE_LIMITED", "Please retry", 30)
    service = build_service(backend, Settings(allow_all_chats=True))
    result = await service.get_messages(["1:1", "2:2", "1:1"])
    assert [m.message_ref.key for m in result.messages] == ["2:2"]
    assert [
        (issue.input_index, issue.code, issue.retry_after_seconds) for issue in result.issues
    ] == [
        (0, "RATE_LIMITED", 30),
        (2, "RATE_LIMITED", 30),
    ]
    assert backend.batch_reads == [("1", [1]), ("2", [2])]


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"])
async def test_retryable_source_failure_preserves_order_and_resumes_after_cooldown(
    code: str,
) -> None:
    clock = FakeClock()
    backend = FakeBackend([msg("1", 1), msg("2", 2)])
    backend.faults["2"] = ToolError(code, "Please retry", 20)
    service = build_service(backend, Settings(allowed_chat_ids=["1", "2"]), clock=clock)
    first = await service.search_messages(query="budget")
    assert first.messages == []
    assert first.next_cursor and not first.scope.complete
    assert set(first.scope.pending_chat_ids) == {"1", "2"}
    assert [(issue.chat_id, issue.code) for issue in first.issues] == [("2", code)]
    del backend.faults["2"]
    clock.time += 1
    waiting = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert not waiting.messages and waiting.next_cursor
    assert backend.fetches == ["1", "2"]
    clock.time += 20
    resumed = await service.search_messages(query="budget", cursor=waiting.next_cursor)
    assert [m.message_ref.key for m in resumed.messages] == ["2:2", "1:1"]
    assert resumed.scope.complete
    assert not resumed.issues and resumed.next_cursor is None
    assert backend.fetch_offsets == [("1", 0), ("2", 0), ("2", 0)]


async def test_permanent_issue_survives_later_successful_pages() -> None:
    backend = FakeBackend([msg("1", mid) for mid in range(1, 4)] + [msg("2", 4)])
    backend.faults["2"] = ToolError("ACCESS_DENIED", "Unavailable source")
    service = build_service(backend, Settings(allowed_chat_ids=["1", "2"]))
    first = await service.search_messages(query="budget", limit=1)
    assert first.next_cursor
    del backend.faults["2"]
    final = await service.search_messages(query="budget", cursor=first.next_cursor)
    assert [m.message_ref.message_id for m in first.messages + final.messages] == [3, 2, 1]
    assert final.next_cursor is None and not final.scope.complete
    assert [(issue.code, issue.chat_id) for issue in final.issues] == [("ACCESS_DENIED", "2")]
    assert backend.fetches.count("2") == 1


@pytest.mark.parametrize("order", ["newest_first", "oldest_first"])
async def test_history_date_window_remains_exact_across_rpc_and_output_pages(
    order: Literal["newest_first", "oldest_first"],
) -> None:
    backend = FakeBackend([msg("1", mid) for mid in range(1, 151)])
    service = build_service(backend, Settings(allow_all_chats=True, rpc_pages_per_call=1))

    async def history(cursor: str | None = None) -> Page:
        return await service.get_history(
            chat_id="1",
            date_from=BASE + timedelta(minutes=40),
            date_to=BASE + timedelta(minutes=91),
            order=order,
            limit=7,
            cursor=cursor,
        )

    page = await history()
    found = list(page.messages)
    for _ in range(40):
        if page.next_cursor is None:
            break
        page = await history(page.next_cursor)
        found.extend(page.messages)
    else:
        pytest.fail("Date-filtered history did not exhaust")
    expected = list(range(40, 91))
    if order == "newest_first":
        expected.reverse()
    assert [m.message_ref.message_id for m in found] == expected
    assert page.scope.complete
    assert len(backend.fetches) > 1


async def test_history_snapshot_cutoff_excludes_new_messages_on_resume() -> None:
    backend = FakeBackend([msg("1", mid) for mid in range(1, 40)])
    service = build_service(backend, Settings(allow_all_chats=True, rpc_pages_per_call=1))
    first = await service.get_history(chat_id="1", order="oldest_first", limit=30)
    assert first.next_cursor
    added = msg("1", 40)
    added.sent_at = first.scope.resolved_at + timedelta(seconds=1)
    backend.stored_messages.append(added)
    last = await service.get_history(
        chat_id="1", order="oldest_first", limit=30, cursor=first.next_cursor
    )
    assert [m.message_ref.message_id for m in first.messages + last.messages] == list(range(1, 40))
    assert last.scope.complete


async def test_unicode_message_chunks_use_character_offsets_and_reconstruct_exact_text() -> None:
    original = "Привет 👩🏽‍💻 e\u0301 🇷🇺\n" * 7
    backend = FakeBackend([msg("1", 1, text=original)])
    service = build_service(backend, Settings(allow_all_chats=True))
    offset = 0
    pieces = []
    while True:
        result = await service.get_messages(["1:1"], text_offset=offset, text_limit=5)
        message = result.messages[0]
        assert message.text_offset == offset
        assert message.text_total_length == len(original)
        assert message.text == original[offset : offset + 5]
        pieces.append(message.text)
        if message.next_text_offset is None:
            break
        assert message.next_text_offset == offset + 5
        offset = message.next_text_offset
    assert "".join(pieces) == original
