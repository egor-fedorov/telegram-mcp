from datetime import timedelta

import pytest

from telegram_mcp.config import Settings
from telegram_mcp.models import Chat, Folder, MessageRef, ToolError
from telegram_mcp.service import Service
from tests.fakes import BASE, FakeBackend, msg


async def test_global_pagination_no_losses_duplicates_or_reordering() -> None:
    messages = [
        msg(cid, i, minute=2 * i + extra)
        for cid, extra in (("1", 0), ("2", 1))
        for i in range(1, 66)
    ]
    service = Service(FakeBackend(messages), Settings(allow_all_chats=True))
    page = await service.query(query="budget", limit=7)
    found = list(page.messages)
    first = page
    while page.next_cursor:
        page = await service.query(query="budget", limit=7, cursor=page.next_cursor)
        found.extend(page.messages)
    assert len(found) == 130
    assert len({m.message_ref.key for m in found}) == 130
    assert [m.sent_at for m in found] == sorted((m.sent_at for m in messages), reverse=True)
    assert page.scope.complete
    replay = await service.query(query="budget", limit=7, cursor=first.next_cursor)
    assert [m.message_ref.key for m in replay.messages] == [m.message_ref.key for m in found[7:14]]


async def test_empty_pages_while_priming_are_resumable() -> None:
    service = Service(
        FakeBackend([msg(str(i), i) for i in range(1, 5)]),
        Settings(allow_all_chats=True, rpc_pages_per_call=1),
    )
    page = await service.query(query="budget")
    assert page.messages == []
    assert page.next_cursor and not page.scope.complete
    for _ in range(3):
        page = await service.query(query="budget", cursor=page.next_cursor)
    assert [m.message_ref.message_id for m in page.messages] == [4, 3, 2, 1]
    assert page.scope.complete


async def test_content_filter_advances_even_when_page_has_no_matches() -> None:
    service = Service(
        FakeBackend([msg("1", i, content="voice" if i == 1 else "text") for i in range(1, 65)]),
        Settings(allow_all_chats=True, rpc_pages_per_call=1),
    )
    page = await service.query(query="budget", content_type="voice")
    assert not page.messages and page.next_cursor
    while page.next_cursor:
        page = await service.query(query="budget", content_type="voice", cursor=page.next_cursor)
    assert [m.message_ref.message_id for m in page.messages] == [1]


async def test_dates_and_oldest_first_history() -> None:
    service = Service(
        FakeBackend([msg("1", i) for i in range(1, 8)]), Settings(allow_all_chats=True)
    )
    page = await service.query(
        kind="history",
        chat_ids=["1"],
        date_from=BASE + timedelta(minutes=2),
        date_to=BASE + timedelta(minutes=5),
        order="oldest_first",
    )
    assert [m.message_ref.message_id for m in page.messages] == [2, 3, 4]
    with pytest.raises(ToolError, match="timezone"):
        await service.query(query="budget", date_from=BASE.replace(tzinfo=None))
    with pytest.raises(ToolError, match="date_from"):
        await service.query(query="budget", date_from=BASE, date_to=BASE)


async def test_cursor_query_binding() -> None:
    service = Service(
        FakeBackend([msg("1", i) for i in range(1, 5)]), Settings(allow_all_chats=True)
    )
    page = await service.query(query="budget", limit=1)
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.query(query="other", cursor=page.next_cursor)
    with pytest.raises(ToolError, match="CURSOR_EXPIRED"):
        await service.query(query="budget", cursor="invented")


async def test_folder_snapshot_and_permission_revocation() -> None:
    folder = Folder(id=1, title="Work", position=0, included_chat_ids=["1", "2"])
    backend = FakeBackend([msg("1", 1), msg("2", 2)], folders=[folder])
    service = Service(backend, Settings(allow_all_chats=True))
    page = await service.list_chats(folder_ids=[1], limit=1)
    folder.included_chat_ids = ["2"]
    continued = await service.list_chats(folder_ids=[1], limit=1, cursor=page.next_cursor)
    assert continued.scope.chat_ids == ["1", "2"]
    fresh = await service.list_chats(folder_ids=[1])
    assert fresh.scope.chat_ids == ["2"]
    folder.included_chat_ids = ["1", "2"]
    restricted = Service(backend, Settings(allowed_folder_ids=[1]))
    first = await restricted.query(query="budget", limit=1)
    folder.included_chat_ids = ["2"]
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await restricted.query(query="budget", cursor=first.next_cursor)


async def test_failures_are_explicit_and_other_sources_continue() -> None:
    backend = FakeBackend([msg("1", 1), msg("2", 2)])
    backend.faults["2"] = ToolError("RATE_LIMITED", "Please retry", 20)
    page = await Service(backend, Settings(allow_all_chats=True)).query(query="budget")
    assert [m.chat.id for m in page.messages] == ["1"]
    assert not page.scope.complete
    assert page.issues[0].retry_after_seconds == 20
    assert page.issues[0].chat_id == "2"


async def test_context_is_chronological_and_blocks_cross_chat_reads() -> None:
    parent = MessageRef(chat_id="2", message_id=1)
    backend = FakeBackend(
        [msg("1", i, reply_to=parent if i == 3 else None) for i in range(1, 6)] + [msg("2", 1)]
    )
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    context = await service.get_context("1:3", before=2, after=2)
    assert [m.message_ref.message_id for m in context.before] == [1, 2]
    assert [m.message_ref.message_id for m in context.after] == [4, 5]
    assert context.issues[0].code == "ACCESS_DENIED"
    assert "2:1" not in backend.reads
    result = await service.get_messages([parent])
    assert not result.messages and result.issues[0].code == "ACCESS_DENIED"


async def test_missing_messages_and_complete_text_retrieval() -> None:
    backend = FakeBackend([msg("1", 1, text="a" * 15000)])
    service = Service(backend, Settings(allow_all_chats=True))
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
    service = Service(backend, Settings(allowed_chat_ids=["-1001"]))
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
    page = await Service(backend, Settings(allow_all_chats=True)).get_thread("-1001:1")
    assert page.thread_root == root
    assert page.messages[0].message_ref.message_id == 11
