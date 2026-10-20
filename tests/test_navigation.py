from datetime import timedelta

import pytest

from telegram_mcp.backend import TopicBatch, TopicOffset
from telegram_mcp.config import Settings
from telegram_mcp.models import Chat, ChatType, Folder, ForumTopic, Issue, ToolError, TopicPage
from telegram_mcp.service import Service
from tests.fakes import BASE, FakeBackend, msg


def topic(topic_id: int, *, chat_id: str = "1") -> ForumTopic:
    return ForumTopic(
        id=topic_id,
        chat_id=chat_id,
        title=f"Topic {topic_id}",
        created_at=BASE + timedelta(minutes=topic_id),
        is_general=topic_id == 1,
    )


def topic_batch(
    ids: list[int],
    *,
    next_offset: TopicOffset | None = None,
    exhausted: bool = True,
    creation_order: bool = False,
    total: int = 20,
    issues: list[Issue] | None = None,
) -> TopicBatch:
    return TopicBatch(
        topics=[topic(topic_id) for topic_id in ids],
        next_offset=next_offset,
        exhausted=exhausted,
        order_by_create_date=creation_order,
        telegram_total_count=total,
        issues=issues or [],
    )


def forum_backend() -> FakeBackend:
    return FakeBackend(
        chats=[
            Chat(
                id="1",
                title="Forum",
                type="group",
                username="forum",
                is_supergroup=True,
                is_forum=True,
            )
        ]
    )


def topic_ids(page: TopicPage) -> list[int]:
    # Tests use the public page, never its cursor's internal state.
    return [item.id for item in page.topics]


@pytest.mark.parametrize("chat_ref", ["", " ", "\n\t"])
async def test_navigation_rejects_blank_source_before_backend_calls(chat_ref: str) -> None:
    backend = forum_backend()
    service = Service(backend, Settings(allow_all_chats=True))
    for method in (service.list_topics, service.get_pinned_messages):
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await method(chat_ref=chat_ref)
    assert backend.backend_calls == []


@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_navigation_rejects_invalid_limits_before_backend_calls(limit: int) -> None:
    backend = forum_backend()
    service = Service(backend, Settings(allow_all_chats=True))
    for method in (service.list_topics, service.get_pinned_messages):
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await method(chat_ref="1", limit=limit)
    assert backend.backend_calls == []


@pytest.mark.parametrize("query", ["", " \n", "x" * 1001])
async def test_topics_reject_invalid_search_before_backend_calls(query: str) -> None:
    backend = forum_backend()
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await Service(backend, Settings(allow_all_chats=True)).list_topics("1", query=query)
    assert backend.backend_calls == []


@pytest.mark.parametrize("reference", ["1", "@forum", "https://t.me/forum"])
async def test_topics_load_catalog_then_resolve_fresh_source(reference: str) -> None:
    backend = forum_backend()
    backend.topic_batches[("1", TopicOffset())] = topic_batch([1], total=1)
    page = await Service(backend, Settings(allowed_chat_ids=["1"])).list_topics(
        chat_ref=reference, query="  release 🚀  ", limit=100
    )
    assert topic_ids(page) == [1]
    assert page.scope.chat_ids == ["1"]
    assert page.scope.complete
    assert page.next_cursor is None
    assert backend.backend_calls[:2] == ["catalog", "resolve_chat"]
    assert backend.resolve_calls[0] == (reference, True)
    assert backend.topic_fetches[0][3] == "release 🚀"


async def test_topics_accept_query_maximum_and_trim_source() -> None:
    backend = forum_backend()
    await Service(backend, Settings(allow_all_chats=True)).list_topics(
        chat_ref="  @forum  ", query=" " + "я" * 1000 + " ", limit=1
    )
    assert backend.resolve_calls[0] == ("@forum", True)
    assert backend.topic_fetches[0][3] == "я" * 1000


@pytest.mark.parametrize("creation_order", [False, True])
async def test_topic_pages_preserve_server_order_buffer_overflow_and_replay(
    creation_order: bool,
) -> None:
    backend = forum_backend()
    backend.topic_batches[("1", TopicOffset())] = topic_batch(
        [3, 1, 8, 5], total=91, creation_order=creation_order
    )
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    first = await service.list_topics("@forum", limit=1)
    assert topic_ids(first) == [3]
    assert first.next_cursor and not first.scope.complete
    assert first.scope.pending_chat_ids == ["1"]
    assert first.telegram_total_count == 91
    assert first.order == ("creation_date" if creation_order else "last_message_date")
    continued = await service.list_topics("@forum", limit=2, cursor=first.next_cursor)
    assert topic_ids(continued) == [1, 8]
    assert continued.next_cursor
    final = await service.list_topics("@forum", limit=100, cursor=continued.next_cursor)
    assert topic_ids(final) == [5]
    assert final.scope.complete and final.next_cursor is None
    assert final.telegram_total_count == 91
    assert len(backend.topic_fetches) == 1
    replay = await service.list_topics("@forum", limit=2, cursor=first.next_cursor)
    assert topic_ids(replay) == [1, 8]
    assert len(backend.topic_fetches) == 1


async def test_topic_short_pages_and_duplicates_advance_without_extra_fetches() -> None:
    backend = forum_backend()
    offsets = [
        TopicOffset(),
        TopicOffset(date=BASE, message_id=80, topic_id=3),
        TopicOffset(date=BASE - timedelta(minutes=1), message_id=70, topic_id=4),
        TopicOffset(date=BASE - timedelta(minutes=2), message_id=60, topic_id=5),
    ]
    for index, ids in enumerate(([3, 3], [3, 4], [4])):
        backend.topic_batches[("1", offsets[index])] = topic_batch(
            list(ids), next_offset=offsets[index + 1], exhausted=False, total=17
        )
    backend.topic_batches[("1", offsets[-1])] = topic_batch([], total=17)
    service = Service(backend, Settings(allow_all_chats=True, rpc_pages_per_call=10))
    cursor = None
    found = []
    for index, expected in enumerate(([3], [4], [], [])):
        page = await service.list_topics("1", limit=100, cursor=cursor)
        assert topic_ids(page) == expected
        found.extend(topic_ids(page))
        assert len(backend.topic_fetches) == index + 1
        assert backend.topic_fetches[-1][1] == offsets[index]
        assert page.telegram_total_count == 17
        cursor = page.next_cursor
        if index < 3:
            assert cursor and not page.scope.complete
        else:
            assert cursor is None and page.scope.complete
    assert found == [3, 4]


async def test_topic_empty_result_is_complete_without_fabricated_topics() -> None:
    backend = forum_backend()
    page = await Service(backend, Settings(allow_all_chats=True)).list_topics("1")
    assert topic_ids(page) == []
    assert page.telegram_total_count == 0
    assert page.next_cursor is None
    assert page.scope.complete and not page.issues
    assert len(backend.topic_fetches) == 1


async def test_empty_terminal_topic_batch_keeps_creation_order() -> None:
    backend = forum_backend()
    offset = TopicOffset(date=BASE, message_id=90, topic_id=3)
    backend.topic_batches[("1", TopicOffset())] = topic_batch(
        [3], next_offset=offset, exhausted=False, creation_order=True, total=1
    )
    # Empty terminal responses may omit the flag used by the preceding nonempty pages.
    backend.topic_batches[("1", offset)] = topic_batch([], total=1)
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.list_topics("1")
    assert first.order == "creation_date" and first.next_cursor
    final = await service.list_topics("1", cursor=first.next_cursor)
    assert final.topics == [] and final.next_cursor is None
    assert final.scope.complete and final.issues == []
    assert final.order == "creation_date"
    assert final.telegram_total_count == 1
    assert len(backend.topic_fetches) == 2


async def test_topic_navigation_preserves_general_short_and_deleted_metadata() -> None:
    backend = forum_backend()
    general = ForumTopic(
        id=1, chat_id="1", title="Renamed lobby", is_general=True, is_hidden=True, is_closed=True
    )
    short = ForumTopic(id=3, chat_id="1", title="Partial topic", is_short=True)
    deleted = ForumTopic(id=4, chat_id="1", is_deleted=True)
    backend.topic_batches[("1", TopicOffset())] = TopicBatch(
        topics=[general, short, deleted],
        next_offset=None,
        exhausted=True,
        order_by_create_date=False,
        telegram_total_count=3,
    )
    page = await Service(backend, Settings(allow_all_chats=True)).list_topics("1")
    assert page.topics == [general, short, deleted]
    assert all(item.root_message_ref is None for item in page.topics)
    assert all(item.top_message_ref is None for item in page.topics)
    assert short.unread_count is None and short.is_pinned is None
    assert deleted.title is None and deleted.created_at is None
    assert backend.thread_calls == 0 and backend.reads == []


async def test_topic_backend_issues_remain_visible_when_buffer_is_drained() -> None:
    backend = forum_backend()
    backend.topic_batches[("1", TopicOffset())] = topic_batch(
        [3, 4],
        total=3,
        issues=[Issue(code="TOPIC_UNAVAILABLE", message="Missing topic metadata.", chat_id="1")],
    )
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.list_topics("1", limit=1)
    assert topic_ids(first) == [3] and first.next_cursor
    final = await service.list_topics("1", cursor=first.next_cursor)
    assert topic_ids(final) == [4]
    assert final.next_cursor is None and not final.scope.complete
    assert [issue.code for issue in final.issues] == ["TOPIC_UNAVAILABLE"]


@pytest.mark.parametrize("mutation", ["alias", "query", "new_owner"])
async def test_topic_cursor_binds_source_reference_identity_and_query(mutation: str) -> None:
    backend = forum_backend()
    backend.topic_batches[("1", TopicOffset())] = topic_batch([3, 4])
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.list_topics("@forum", query="release", limit=1)
    reference, query = "@forum", "release"
    if mutation == "alias":
        reference = "1"
    elif mutation == "query":
        query = "changed"
    else:
        backend.chats[0].username = "old_forum"
        backend.chats.append(
            Chat(id="2", title="New owner", type="group", username="forum", is_forum=True)
        )
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.list_topics(reference, query=query, cursor=first.next_cursor)
    assert len(backend.topic_fetches) == 1


@pytest.mark.parametrize("failure", ["revoked", "not_forum", "catalog", "resolver"])
async def test_topic_resume_revalidates_before_buffer_and_keeps_checkpoint(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = forum_backend()
    backend.topic_batches[("1", TopicOffset())] = topic_batch([3, 4])
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    first = await service.list_topics("@forum", limit=1)
    original_resolver = backend.resolve_chat
    if failure == "revoked":
        service.settings.allowed_chat_ids.clear()
    elif failure == "not_forum":
        backend.chats[0].is_forum = False
    elif failure == "catalog":
        backend.catalog_fault = ToolError("CONNECTION_ERROR", "Catalog temporarily unavailable.")
    else:

        async def unavailable(ref: str, *, refresh: bool = False) -> Chat:
            raise ToolError("TELEGRAM_UNAVAILABLE", "Metadata temporarily unavailable.")

        monkeypatch.setattr(backend, "resolve_chat", unavailable)
    with pytest.raises(ToolError):
        await service.list_topics("@forum", cursor=first.next_cursor)
    assert len(backend.topic_fetches) == 1
    service.settings.allowed_chat_ids = ["1"]
    backend.chats[0].is_forum = True
    backend.catalog_fault = None
    monkeypatch.setattr(backend, "resolve_chat", original_resolver)
    resumed = await service.list_topics("@forum", cursor=first.next_cursor)
    assert topic_ids(resumed) == [4]
    assert resumed.scope.complete


async def test_topic_folder_permission_is_rechecked_before_buffer() -> None:
    backend = forum_backend()
    folder = Folder(id=8, title="Forums", position=0, included_chat_ids=["1"])
    backend.folders = [folder]
    backend.topic_batches[("1", TopicOffset())] = topic_batch([3, 4])
    service = Service(backend, Settings(allowed_folder_ids=[8]))
    first = await service.list_topics("1", limit=1)
    folder.included_chat_ids = []
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.list_topics("1", cursor=first.next_cursor)
    assert len(backend.topic_fetches) == 1


@pytest.mark.parametrize("discovery", [False, True])
async def test_public_read_and_discovery_flags_do_not_grant_forum_access(discovery: bool) -> None:
    backend = forum_backend()
    service = Service(
        backend,
        Settings(
            allow_public_channel_reads=True,
            allow_public_channel_discovery=discovery,
        ),
    )
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.list_topics("@forum")
    assert backend.public_checks == []
    assert backend.topic_fetches == []
    service.settings.allowed_chat_ids.append("1")
    page = await service.list_topics("@forum")
    assert page.scope.complete


@pytest.mark.parametrize("chat_type", ["channel", "private", "bot", "group"])
async def test_topic_navigation_rejects_nonforum_chat_types(chat_type: ChatType) -> None:
    backend = FakeBackend(chats=[Chat(id="1", title="Not a forum", type=chat_type)])
    with pytest.raises(ToolError):
        await Service(backend, Settings(allow_all_chats=True)).list_topics("1")
    assert backend.topic_fetches == []


async def test_topics_require_supergroup_even_when_forum_flag_is_set() -> None:
    backend = forum_backend()
    backend.chats[0].is_supergroup = False
    with pytest.raises(ToolError, match="FORUM_UNAVAILABLE"):
        await Service(backend, Settings(allow_all_chats=True)).list_topics("1")
    assert backend.topic_fetches == []


async def test_first_topic_fetch_failure_does_not_fabricate_count_or_order() -> None:
    backend = forum_backend()
    backend.topic_faults["1"] = ToolError("CONNECTION_ERROR", "Unable to fetch topic metadata.")
    service = Service(backend, Settings(allow_all_chats=True))
    failed = await service.list_topics("1")
    assert failed.topics == [] and failed.next_cursor
    assert failed.telegram_total_count is None and failed.order is None
    assert not failed.scope.complete and failed.scope.pending_chat_ids == ["1"]
    del backend.topic_faults["1"]
    recovered = await service.list_topics("1", cursor=failed.next_cursor)
    assert recovered.scope.complete and recovered.issues == []
    assert recovered.telegram_total_count == 0


@pytest.mark.parametrize(
    ("code", "delay"),
    [("RATE_LIMITED", 10), ("CONNECTION_ERROR", None), ("TELEGRAM_UNAVAILABLE", None)],
)
async def test_topic_fetch_transients_preserve_offset_and_clear_after_retry(
    code: str, delay: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr("telegram_mcp.service.time.monotonic", lambda: now[0])
    backend = forum_backend()
    offset = TopicOffset(date=BASE, message_id=90, topic_id=3)
    backend.topic_batches[("1", TopicOffset())] = topic_batch(
        [3], next_offset=offset, exhausted=False
    )
    backend.topic_batches[("1", offset)] = topic_batch([4], total=2)
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.list_topics("1")
    backend.topic_faults["1"] = ToolError(code, "Temporary topic failure.", retry_after=delay)
    failed = await service.list_topics("1", cursor=first.next_cursor)
    assert topic_ids(failed) == []
    assert failed.next_cursor and not failed.scope.complete
    assert [issue.code for issue in failed.issues] == [code]
    assert failed.issues[0].chat_id == "1"
    assert backend.topic_fetches[-1][1] == offset
    assert len(backend.topic_fetches) == 2
    del backend.topic_faults["1"]
    if delay:
        now[0] += 2
        waiting = await service.list_topics("1", cursor=failed.next_cursor)
        assert topic_ids(waiting) == []
        assert waiting.next_cursor
        assert waiting.issues[0].retry_after_seconds == 8
        assert len(backend.topic_fetches) == 2
        now[0] += 8
    recovered = await service.list_topics("1", cursor=failed.next_cursor)
    assert topic_ids(recovered) == [4]
    assert backend.topic_fetches[-1][1] == offset
    assert recovered.issues == []
    assert recovered.scope.complete and recovered.next_cursor is None


async def test_topic_permanent_failure_does_not_invalidate_previous_checkpoint() -> None:
    backend = forum_backend()
    offset = TopicOffset(date=BASE, message_id=90, topic_id=3)
    backend.topic_batches[("1", TopicOffset())] = topic_batch(
        [3], next_offset=offset, exhausted=False
    )
    backend.topic_batches[("1", offset)] = topic_batch([4])
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.list_topics("1")
    backend.topic_faults["1"] = ToolError("ACCESS_DENIED", "Forum unavailable.")
    failed = await service.list_topics("1", cursor=first.next_cursor)
    assert not failed.scope.complete and failed.next_cursor is None
    assert [issue.code for issue in failed.issues] == ["ACCESS_DENIED"]
    del backend.topic_faults["1"]
    retried = await service.list_topics("1", cursor=first.next_cursor)
    assert topic_ids(retried) == [4]


@pytest.mark.parametrize("reason", ["stalled", "order_changed"])
async def test_topic_pagination_inconsistency_is_explicit_and_does_not_loop(reason: str) -> None:
    backend = forum_backend()
    offset = TopicOffset(date=BASE, message_id=90, topic_id=3)
    backend.topic_batches[("1", TopicOffset())] = topic_batch(
        [3], next_offset=offset, exhausted=False
    )
    backend.topic_batches[("1", offset)] = topic_batch(
        [4],
        next_offset=offset if reason == "stalled" else TopicOffset(topic_id=4),
        exhausted=False,
        creation_order=reason == "order_changed",
    )
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.list_topics("1")
    failed = await service.list_topics("1", cursor=first.next_cursor)
    assert failed.topics == []
    assert failed.next_cursor is None and not failed.scope.complete
    assert [issue.code for issue in failed.issues] == [
        "PAGINATION_STALLED" if reason == "stalled" else "PAGINATION_CHANGED"
    ]
    assert len(backend.topic_fetches) == 2


async def test_topic_offset_cycle_cannot_keep_cursor_alive_indefinitely() -> None:
    backend = forum_backend()
    first_offset = TopicOffset(date=BASE, message_id=90, topic_id=3)
    second_offset = TopicOffset(date=BASE - timedelta(minutes=1), message_id=80, topic_id=4)
    backend.topic_batches[("1", TopicOffset())] = topic_batch(
        [3], next_offset=first_offset, exhausted=False
    )
    backend.topic_batches[("1", first_offset)] = topic_batch(
        [4], next_offset=second_offset, exhausted=False
    )
    backend.topic_batches[("1", second_offset)] = topic_batch(
        [5], next_offset=first_offset, exhausted=False
    )
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.list_topics("1")
    second = await service.list_topics("1", cursor=first.next_cursor)
    cycle = await service.list_topics("1", cursor=second.next_cursor)
    assert cycle.topics == [] and cycle.next_cursor is None
    assert not cycle.scope.complete
    assert [issue.code for issue in cycle.issues] == ["PAGINATION_STALLED"]
    assert len(backend.topic_fetches) == 3


async def test_topic_missing_continuation_offset_is_explicit_not_a_restart() -> None:
    backend = forum_backend()
    backend.topic_batches[("1", TopicOffset())] = topic_batch([3], exhausted=False)
    page = await Service(backend, Settings(allow_all_chats=True)).list_topics("1")
    assert page.topics == [] and page.next_cursor is None
    assert not page.scope.complete
    assert [issue.code for issue in page.issues] == ["PAGINATION_STALLED"]
    assert len(backend.topic_fetches) == 1


async def test_pinned_messages_are_chat_wide_paginated_and_history_stays_unfiltered() -> None:
    backend = FakeBackend([msg("1", mid) for mid in range(1, 70)])
    backend.pinned_message_keys = {f"1:{mid}" for mid in range(1, 70, 2)}
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    page = await service.get_pinned_messages("1", limit=7)
    first_cursor = page.next_cursor
    found = list(page.messages)
    while page.next_cursor:
        page = await service.get_pinned_messages("1", limit=7, cursor=page.next_cursor)
        found.extend(page.messages)
    assert [message.message_ref.message_id for message in found] == list(range(69, 0, -2))
    assert page.scope.complete
    assert all(backend.fetch_pinned_flags)
    replay = await service.get_pinned_messages("1", limit=7, cursor=first_cursor)
    assert [message.message_ref for message in replay.messages] == [
        message.message_ref for message in found[7:14]
    ]
    history = await service.get_history(chat_ref="1", limit=3)
    assert [message.message_ref.message_id for message in history.messages] == [69, 68, 67]
    assert backend.fetch_pinned_flags[-1] is False


async def test_empty_pins_are_complete_and_do_not_return_regular_messages() -> None:
    backend = FakeBackend([msg("1", 1)])
    page = await Service(backend, Settings(allowed_chat_ids=["1"])).get_pinned_messages("1")
    assert page.messages == []
    assert page.next_cursor is None and page.scope.complete
    assert backend.fetch_pinned_flags == [True]


@pytest.mark.parametrize("reference", ["-1009", "@news", "https://t.me/news"])
async def test_pins_allow_explicit_public_broadcast_without_widening_scope(reference: str) -> None:
    public = Chat(id="-1009", title="News", type="channel", username="news")
    backend = FakeBackend(
        [msg("1", 1), msg(public.id, 2)],
        chats=[Chat(id="1", title="Allowed", type="group")],
        external_chats=[public],
    )
    backend.pinned_message_keys = {"1:1", "-1009:2"}
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_pinned_messages(reference)
    assert backend.fetches == []
    service.settings.allow_public_channel_reads = True
    page = await service.get_pinned_messages(reference)
    assert [message.message_ref.key for message in page.messages] == ["-1009:2"]
    assert page.scope.chat_ids == ["-1009"]
    assert backend.public_checks == ["-1009"]
    assert (reference, True) in backend.resolve_calls
    assert [chat.id for chat in (await service.list_chats()).chats] == ["1"]
    assert (await service.query(query="budget")).scope.chat_ids == ["1"]


@pytest.mark.parametrize("change", ["disabled", "private"])
async def test_pins_recheck_public_permission_before_emitting_buffer(change: str) -> None:
    public = Chat(id="-1009", title="News", type="channel", username="news")
    backend = FakeBackend([msg(public.id, 1), msg(public.id, 2)], chats=[], external_chats=[public])
    backend.pinned_message_keys = {"-1009:1", "-1009:2"}
    service = Service(backend, Settings(allow_public_channel_reads=True))
    first = await service.get_pinned_messages("@news", limit=1)
    if change == "disabled":
        service.settings.allow_public_channel_reads = False
    else:
        backend.public_faults[public.id] = ToolError("ACCESS_DENIED", "Channel is now private.")
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_pinned_messages("@news", cursor=first.next_cursor)
    assert backend.fetches == [public.id]
    service.settings.allow_public_channel_reads = True
    backend.public_faults.clear()
    recovered = await service.get_pinned_messages("@news", cursor=first.next_cursor)
    assert [message.message_ref.message_id for message in recovered.messages] == [1]


async def test_navigation_cursor_namespaces_and_pinned_source_binding() -> None:
    backend = forum_backend()
    backend.stored_messages = [msg("1", mid) for mid in range(1, 4)]
    backend.pinned_message_keys = {"1:1", "1:3"}
    backend.topic_batches[("1", TopicOffset())] = topic_batch([3, 4])
    service = Service(backend, Settings(allow_all_chats=True))
    pins = await service.get_pinned_messages("@forum", limit=1)
    topics = await service.list_topics("@forum", limit=1)
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_history(chat_ref="@forum", cursor=pins.next_cursor)
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_pinned_messages("@forum", cursor=topics.next_cursor)
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.list_topics("@forum", cursor=pins.next_cursor)
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_pinned_messages("1", cursor=pins.next_cursor)
    backend.chats[0].username = "old_forum"
    backend.chats.append(Chat(id="2", title="New owner", type="group", username="forum"))
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_pinned_messages("@forum", cursor=pins.next_cursor)


@pytest.mark.parametrize(
    ("code", "delay"),
    [("RATE_LIMITED", 9), ("CONNECTION_ERROR", None), ("TELEGRAM_UNAVAILABLE", None)],
)
async def test_pins_transient_failure_retains_cursor_and_filter(
    code: str, delay: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr("telegram_mcp.service.time.monotonic", lambda: now[0])
    backend = FakeBackend([msg("1", 1), msg("1", 2)])
    backend.pinned_message_keys = {"1:1"}
    backend.faults["1"] = ToolError(code, "Pinned messages unavailable.", retry_after=delay)
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    failed = await service.get_pinned_messages("1")
    assert failed.messages == [] and failed.next_cursor
    assert [issue.code for issue in failed.issues] == [code]
    del backend.faults["1"]
    if delay:
        pending = await service.get_pinned_messages("1", cursor=failed.next_cursor)
        assert pending.messages == []
        assert len(backend.fetches) == 1
        now[0] += delay
    recovered = await service.get_pinned_messages("1", cursor=failed.next_cursor)
    assert [message.message_ref.key for message in recovered.messages] == ["1:1"]
    assert recovered.issues == [] and recovered.scope.complete
    assert backend.fetch_offsets == [("1", 0), ("1", 0)]
    assert backend.fetch_pinned_flags == [True, True]
