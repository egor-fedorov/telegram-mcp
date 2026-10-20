from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest

from telegram_mcp.backend import Batch
from telegram_mcp.config import Settings
from telegram_mcp.models import (
    Chat,
    Entity,
    Folder,
    ForumTopic,
    Issue,
    Message,
    MessageRef,
    Page,
    ToolError,
)
from telegram_mcp.service import Service
from tests.fakes import BASE, FakeBackend, msg

Order = Literal["newest_first", "oldest_first"]


def forum_backend(messages: list[Message] | None = None) -> FakeBackend:
    forum = Chat(
        id="1",
        title="Forum",
        username="forum",
        type="group",
        is_supergroup=True,
        is_forum=True,
    )
    backend = FakeBackend(messages, chats=[forum])
    backend.topics = {
        ("1", topic_id): ForumTopic(
            id=topic_id,
            chat_id="1",
            title=f"Topic {topic_id}",
            is_general=topic_id == 1,
            root_message_ref=(
                None if topic_id == 1 else MessageRef(chat_id="1", message_id=topic_id)
            ),
        )
        for topic_id in (1, 10, 20)
    }
    return backend


def message_ids(page: Page) -> list[int]:
    return [message.message_ref.message_id for message in page.messages]


@pytest.mark.parametrize("topic_id", [0, -1, 2147483648, True, False, 1.5, "1"])
@pytest.mark.parametrize("pinned", [False, True])
async def test_invalid_topic_selector_fails_before_backend_calls(
    topic_id: object, pinned: bool
) -> None:
    backend = forum_backend()
    service = Service(backend, Settings(allow_all_chats=True))
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        if pinned:
            await service.get_pinned_messages("1", topic_id=cast(int, topic_id))
        else:
            await service.get_history(chat_id="1", topic_id=cast(int, topic_id))
    assert backend.backend_calls == []


@pytest.mark.parametrize("topic_id", [1, 2147483647])
async def test_topic_selector_accepts_telegram_id_bounds(topic_id: int) -> None:
    backend = forum_backend()
    backend.topics[("1", topic_id)] = ForumTopic(id=topic_id, chat_id="1", is_general=topic_id == 1)
    page = await Service(backend, Settings(allowed_chat_ids=["1"])).get_history(
        chat_id="1", topic_id=topic_id
    )
    assert page.topic_id == topic_id
    assert page.messages == [] and page.scope.complete
    assert backend.topic_lookups == [("1", topic_id)]


@pytest.mark.parametrize("reference", ["legacy_id", "1", "@forum", "https://t.me/forum"])
async def test_topic_history_resolves_every_source_fresh_before_lookup(reference: str) -> None:
    backend = forum_backend([msg("1", 21, topic_id=10), msg("1", 22, topic_id=20)])
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    if reference == "legacy_id":
        page = await service.get_history(chat_id="1", topic_id=10)
    else:
        page = await service.get_history(chat_ref=reference, topic_id=10)
    assert message_ids(page) == [21]
    assert page.topic_id == 10 and page.scope.chat_ids == ["1"]
    assert backend.backend_calls[:3] == ["catalog", "resolve_chat", "get_topic"]
    assert backend.resolve_calls[0] == ("1" if reference == "legacy_id" else reference, True)
    assert backend.topic_lookups == [("1", 10)]
    assert backend.fetch_topic_ids == [10]
    assert backend.reads == [] and backend.thread_calls == 0


async def test_omitted_topic_preserves_chat_wide_history_and_pins() -> None:
    backend = forum_backend([msg("1", 21, topic_id=1), msg("1", 22, topic_id=10), msg("1", 23)])
    backend.pinned_message_keys = {"1:21", "1:22"}
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    history = await service.get_history("1")
    pins = await service.get_pinned_messages("1")
    assert message_ids(history) == [23, 22, 21]
    assert message_ids(pins) == [22, 21]
    assert history.topic_id is None and pins.topic_id is None
    assert backend.topic_lookups == []
    assert backend.fetch_topic_ids == [None, None]


@pytest.mark.parametrize("pinned", [False, True])
async def test_public_flags_do_not_grant_forum_topic_access(pinned: bool) -> None:
    backend = forum_backend([msg("1", 21, topic_id=10)])
    backend.pinned_message_keys = {"1:21"}
    service = Service(
        backend, Settings(allow_public_channel_reads=True, allow_public_channel_discovery=True)
    )
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        if pinned:
            await service.get_pinned_messages("@forum", topic_id=10)
        else:
            await service.get_history(chat_ref="@forum", topic_id=10)
    assert backend.public_checks == []
    assert backend.topic_lookups == [] and backend.fetches == []


@pytest.mark.parametrize(
    "chat",
    [
        Chat(id="1", title="Private", type="private", is_forum=True, is_supergroup=True),
        Chat(id="1", title="Bot", type="bot", is_forum=True, is_supergroup=True),
        Chat(id="1", title="Saved", type="saved", is_forum=True, is_supergroup=True),
        Chat(id="1", title="Channel", type="channel", is_forum=True, is_supergroup=True),
        Chat(id="1", title="Small group", type="group", is_forum=True),
        Chat(id="1", title="Supergroup", type="group", is_supergroup=True),
    ],
)
@pytest.mark.parametrize("pinned", [False, True])
async def test_topic_selection_requires_a_supported_forum(chat: Chat, pinned: bool) -> None:
    backend = forum_backend()
    backend.chats = [chat]
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    with pytest.raises(ToolError, match="FORUM_UNAVAILABLE"):
        if pinned:
            await service.get_pinned_messages("1", topic_id=1)
        else:
            await service.get_history(chat_id="1", topic_id=1)
    assert backend.topic_lookups == [] and backend.fetches == []


@pytest.mark.parametrize("invalid", ["missing", "deleted", "foreign_chat", "different_id"])
async def test_topic_lookup_cannot_return_missing_or_mismatched_topic(invalid: str) -> None:
    backend = forum_backend([msg("1", 21, topic_id=10)])
    if invalid == "missing":
        del backend.topics[("1", 10)]
    elif invalid == "deleted":
        backend.topics[("1", 10)].is_deleted = True
    elif invalid == "foreign_chat":
        backend.topics[("1", 10)].chat_id = "2"
    else:
        backend.topics[("1", 10)].id = 20
    with pytest.raises(ToolError, match="TOPIC_UNAVAILABLE"):
        await Service(backend, Settings(allowed_chat_ids=["1"])).get_history(
            chat_id="1", topic_id=10
        )
    assert backend.fetches == []
    assert backend.reads == [] and backend.thread_calls == 0


async def test_hidden_closed_general_is_readable_without_a_fake_root_or_parent_fetch() -> None:
    foreign_parent = MessageRef(chat_id="2", message_id=900)
    missing_parent = MessageRef(chat_id="1", message_id=12)
    backend = forum_backend(
        [
            msg("1", 21, topic_id=1, reply_to=foreign_parent),
            msg("1", 22, topic_id=1, reply_to=missing_parent),
            msg("1", 23, topic_id=10),
            msg("2", 900),
        ]
    )
    backend.topics[("1", 1)].is_hidden = True
    backend.topics[("1", 1)].is_closed = True
    page = await Service(backend, Settings(allowed_chat_ids=["1"])).get_history(
        chat_id="1", topic_id=1
    )
    assert message_ids(page) == [22, 21]
    assert page.topic_id == 1 and page.thread_root is None
    assert page.messages[0].reply_to == missing_parent
    assert page.messages[1].reply_to == foreign_parent
    assert all(message.thread_ref is None for message in page.messages)
    assert page.issues == []
    assert backend.reads == [] and backend.batch_reads == [] and backend.thread_calls == 0


async def test_topic_reads_accept_folder_permissions_and_revalidate_them() -> None:
    backend = forum_backend([msg("1", 21, topic_id=10), msg("1", 22, topic_id=10)])
    folder = Folder(id=7, title="Forums", position=0, included_chat_ids=["1"])
    backend.folders = [folder]
    service = Service(backend, Settings(allowed_folder_ids=[7]))
    first = await service.get_history(chat_id="1", topic_id=10, limit=1)
    assert message_ids(first) == [22] and first.next_cursor
    folder.included_chat_ids = []
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_history(chat_id="1", topic_id=10, cursor=first.next_cursor)
    folder.included_chat_ids = ["1"]
    final = await service.get_history(chat_id="1", topic_id=10, cursor=first.next_cursor)
    assert message_ids(final) == [21]
    assert backend.fetches == ["1"]


@pytest.mark.parametrize("topic_id", [1, 10])
@pytest.mark.parametrize("order", ["newest_first", "oldest_first"])
async def test_topic_date_window_paginates_both_orders_without_other_topics(
    topic_id: int, order: Order
) -> None:
    backend = forum_backend(
        [msg("1", mid, topic_id=topic_id if mid % 2 == 0 else 20) for mid in range(1, 301)]
    )
    service = Service(backend, Settings(allowed_chat_ids=["1"], rpc_pages_per_call=1))

    async def history(cursor: str | None = None, limit: int = 7) -> Page:
        return await service.get_history(
            chat_id="1",
            topic_id=topic_id,
            date_from=BASE + timedelta(minutes=120),
            date_to=BASE + timedelta(minutes=181),
            order=order,
            limit=limit,
            cursor=cursor,
        )

    first = await history()
    assert first.messages == [] and first.next_cursor and not first.scope.complete
    page = first
    found = []
    for _ in range(40):
        if page.next_cursor is None:
            break
        page = await history(page.next_cursor, limit=11)
        assert page.topic_id == topic_id
        found.extend(message_ids(page))
    else:
        pytest.fail("Topic history did not exhaust.")
    expected = list(range(120, 181, 2))
    if order == "newest_first":
        expected.reverse()
    assert found == expected
    assert page.scope.complete and page.issues == []
    assert set(backend.fetch_topic_ids) == {topic_id}
    replay = await history(first.next_cursor, limit=11)
    assert message_ids(replay) == found[: len(replay.messages)]


@pytest.mark.parametrize("explicit_future_end", [False, True])
async def test_topic_history_freezes_upper_date_for_later_pages(explicit_future_end: bool) -> None:
    backend = forum_backend([msg("1", mid, topic_id=1) for mid in range(1, 40)])
    service = Service(backend, Settings(allowed_chat_ids=["1"], rpc_pages_per_call=1))
    end = datetime.now(UTC) + timedelta(days=1) if explicit_future_end else None
    first = await service.get_history(
        chat_id="1", topic_id=1, order="oldest_first", limit=30, date_to=end
    )
    assert first.next_cursor
    added = msg("1", 40, topic_id=1)
    added.sent_at = first.scope.resolved_at + timedelta(seconds=1)
    backend.stored_messages.append(added)
    final = await service.get_history(
        chat_id="1",
        topic_id=1,
        order="oldest_first",
        limit=30,
        date_to=end,
        cursor=first.next_cursor,
    )
    assert message_ids(first) + message_ids(final) == list(range(1, 40))
    assert first.scope.resolved_at == final.scope.resolved_at
    assert final.scope.complete


@pytest.mark.parametrize("topic_id", [1, 10])
async def test_topic_pins_keep_only_pinned_messages_in_selected_topic(topic_id: int) -> None:
    backend = forum_backend(
        [msg("1", mid, topic_id=topic_id if mid % 2 == 0 else 20) for mid in range(21, 151)]
    )
    backend.pinned_message_keys = {f"1:{mid}" for mid in range(21, 151) if mid % 3 == 0}
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    page = await service.get_pinned_messages("@forum", topic_id=topic_id, limit=5)
    found = message_ids(page)
    while page.next_cursor:
        page = await service.get_pinned_messages(
            "@forum", topic_id=topic_id, limit=11, cursor=page.next_cursor
        )
        assert page.topic_id == topic_id
        found.extend(message_ids(page))
    assert found == list(range(150, 23, -6))
    assert page.scope.complete
    assert all(backend.fetch_pinned_flags)
    assert set(backend.fetch_topic_ids) == {topic_id}
    assert backend.thread_calls == 0 and backend.reads == []


async def test_empty_topic_pins_do_not_return_pins_from_general_or_other_topics() -> None:
    backend = forum_backend(
        [msg("1", 21, topic_id=1), msg("1", 22, topic_id=10), msg("1", 23, topic_id=20)]
    )
    backend.pinned_message_keys = {"1:21", "1:23"}
    page = await Service(backend, Settings(allowed_chat_ids=["1"])).get_pinned_messages(
        "1", topic_id=10
    )
    assert page.messages == [] and page.topic_id == 10
    assert page.next_cursor is None and page.scope.complete


async def test_topic_text_budget_preserves_unicode_entities_and_retrievable_remainder() -> None:
    original = "Привет 👩🏽‍💻 e\u0301 🇷🇺\n" * 1000
    message = msg("1", 21, text=original, topic_id=1)
    message.entities = [Entity(type="bold", offset=1100, length=25)]
    message.url = "https://t.me/forum/21"
    backend = forum_backend([message, msg("1", 22, text=original, topic_id=1)])
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    page = await service.get_history(chat_id="1", topic_id=1, limit=100)
    assert sum(len(item.text) for item in page.messages) <= 100000
    clipped = next(item for item in page.messages if item.message_ref.message_id == 21)
    assert clipped.text == original[:1000]
    assert clipped.next_text_offset == 1000
    assert clipped.text_total_length == len(original)
    assert clipped.entities == message.entities
    assert clipped.sent_at == message.sent_at and clipped.url == message.url
    pieces = [clipped.text]
    offset = clipped.next_text_offset
    while offset is not None:
        result = await service.get_messages(["1:21"], text_offset=offset, text_limit=1999)
        part = result.messages[0]
        assert part.topic_id == 1 and part.entities == message.entities
        assert part.text_offset == offset
        pieces.append(part.text)
        offset = part.next_text_offset
    assert "".join(pieces) == original


@pytest.mark.parametrize(
    "change",
    ["topic", "no_topic", "reference", "legacy_id", "order", "date_from", "date_to", "pins"],
)
async def test_topic_history_cursor_binds_every_selector(change: str) -> None:
    backend = forum_backend([msg("1", mid, topic_id=10) for mid in range(21, 24)])
    backend.pinned_message_keys = {"1:21", "1:22", "1:23"}
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    first = await service.get_history(chat_ref="@forum", topic_id=10, limit=1)
    assert first.next_cursor
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        if change == "pins":
            await service.get_pinned_messages("@forum", topic_id=10, cursor=first.next_cursor)
        elif change == "legacy_id":
            await service.get_history(chat_id="1", topic_id=10, cursor=first.next_cursor)
        else:
            await service.get_history(
                chat_ref="1" if change == "reference" else "@forum",
                topic_id=20 if change == "topic" else None if change == "no_topic" else 10,
                order="oldest_first" if change == "order" else "newest_first",
                date_from=BASE if change == "date_from" else None,
                date_to=BASE + timedelta(hours=1) if change == "date_to" else None,
                cursor=first.next_cursor,
            )
    assert backend.fetches == ["1"]
    recovered = await service.get_history(
        chat_ref="@forum", topic_id=10, limit=100, cursor=first.next_cursor
    )
    assert message_ids(recovered) == [22, 21]


async def test_topic_pin_cursor_cannot_be_used_for_chat_wide_pins_or_another_topic() -> None:
    backend = forum_backend([msg("1", mid, topic_id=10) for mid in range(21, 24)])
    backend.pinned_message_keys = {"1:21", "1:22", "1:23"}
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    first = await service.get_pinned_messages("1", topic_id=10, limit=1)
    for topic_id in (None, 1, 20):
        with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
            await service.get_pinned_messages("1", topic_id=topic_id, cursor=first.next_cursor)
    assert backend.fetches == ["1"]


async def test_reassigned_username_cannot_move_topic_cursor_to_another_forum() -> None:
    backend = forum_backend([msg("1", 21, topic_id=10), msg("1", 22, topic_id=10)])
    service = Service(backend, Settings(allow_all_chats=True))
    first = await service.get_history(chat_ref="@forum", topic_id=10, limit=1)
    backend.chats[0].username = "renamed_forum"
    replacement = backend.chats[0].model_copy(update={"id": "2", "username": "forum"})
    backend.chats.append(replacement)
    backend.topics[("2", 10)] = ForumTopic(id=10, chat_id="2")
    with pytest.raises(ToolError, match="CURSOR_MISMATCH"):
        await service.get_history(chat_ref="@forum", topic_id=10, cursor=first.next_cursor)
    assert backend.fetches == ["1"]


async def test_renamed_forum_and_topic_keep_numeric_cursor_identity() -> None:
    backend = forum_backend([msg("1", 21, topic_id=10), msg("1", 22, topic_id=10)])
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    first = await service.get_history(chat_id="1", topic_id=10, limit=1)
    backend.chats[0].username = "new_forum_name"
    backend.topics[("1", 10)].title = "New topic name"
    final = await service.get_history(chat_id="1", topic_id=10, cursor=first.next_cursor)
    assert message_ids(final) == [21]
    assert backend.topic_lookups == [("1", 10), ("1", 10)]
    assert backend.fetches == ["1"]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("access", "ACCESS_DENIED"),
        ("forum", "FORUM_UNAVAILABLE"),
        ("deleted", "TOPIC_UNAVAILABLE"),
        ("missing", "TOPIC_UNAVAILABLE"),
        ("catalog", "CONNECTION_ERROR"),
        ("lookup", "CONNECTION_ERROR"),
        ("lookup_flood", "RATE_LIMITED"),
    ],
)
@pytest.mark.parametrize("pinned", [False, True])
async def test_topic_continuation_revalidates_before_buffer_and_original_cursor_survives(
    change: str, code: str, pinned: bool
) -> None:
    backend = forum_backend([msg("1", 21, topic_id=10), msg("1", 22, topic_id=10)])
    backend.pinned_message_keys = {"1:21", "1:22"}
    service = Service(backend, Settings(allowed_chat_ids=["1"]))

    async def read(cursor: str | None = None) -> Page:
        if pinned:
            return await service.get_pinned_messages("1", topic_id=10, cursor=cursor, limit=1)
        return await service.get_history(chat_id="1", topic_id=10, cursor=cursor, limit=1)

    first = await read()
    assert message_ids(first) == [22] and first.next_cursor
    original_topic = backend.topics[("1", 10)].model_copy()
    if change == "access":
        service.settings.allowed_chat_ids = []
    elif change == "forum":
        backend.chats[0].is_forum = False
    elif change == "deleted":
        backend.topics[("1", 10)].is_deleted = True
    elif change == "missing":
        del backend.topics[("1", 10)]
    elif change == "catalog":
        backend.catalog_fault = ToolError(code, "Catalog unavailable.")
    else:
        backend.topic_lookup_faults[("1", 10)] = ToolError(
            code, "Topic check unavailable.", retry_after=10 if change == "lookup_flood" else None
        )
    with pytest.raises(ToolError, match=code):
        await read(first.next_cursor)
    assert backend.fetches == ["1"]
    service.settings.allowed_chat_ids = ["1"]
    backend.chats[0].is_forum = True
    backend.topics[("1", 10)] = original_topic
    backend.catalog_fault = None
    backend.topic_lookup_faults.clear()
    recovered = await read(first.next_cursor)
    assert message_ids(recovered) == [21]
    assert recovered.scope.complete and recovered.issues == []
    assert backend.fetches == ["1"]


@pytest.mark.parametrize("change", ["forum_disabled", "connection_error"])
async def test_numeric_topic_cursor_requires_fresh_resolution_even_with_stale_catalog(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = forum_backend([msg("1", 21, topic_id=10), msg("1", 22, topic_id=10)])
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    first = await service.get_history(chat_id="1", topic_id=10, limit=1)
    original_resolve = backend.resolve_chat

    async def resolve_chat(ref: str, *, refresh: bool = False) -> Chat:
        assert ref == "1" and refresh
        if change == "connection_error":
            raise ToolError("CONNECTION_ERROR", "Fresh forum metadata is unavailable.")
        return backend.chats[0].model_copy(update={"is_forum": False})

    monkeypatch.setattr(backend, "resolve_chat", resolve_chat)
    with pytest.raises(
        ToolError, match="CONNECTION_ERROR" if change == "connection_error" else "FORUM_UNAVAILABLE"
    ):
        await service.get_history(chat_id="1", topic_id=10, cursor=first.next_cursor)
    assert backend.chats[0].is_forum
    assert backend.fetches == ["1"]
    monkeypatch.setattr(backend, "resolve_chat", original_resolve)
    recovered = await service.get_history(chat_id="1", topic_id=10, cursor=first.next_cursor)
    assert message_ids(recovered) == [21] and recovered.scope.complete


async def test_topic_batch_issues_survive_buffer_and_later_successful_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = forum_backend()
    issue = Issue(
        code="PAGINATION_INCOMPLETE",
        message="Telegram returned a message without confirmed topic membership.",
        chat_id="1",
    )
    batches = [
        Batch([msg("1", 23, topic_id=10), msg("1", 22, topic_id=10)], 22, False, [issue]),
        Batch([msg("1", 21, topic_id=10)], 21, True),
    ]
    requests: list[dict[str, object]] = []

    async def fetch(chat_id: str, **kwargs: object) -> Batch:
        assert chat_id == "1"
        requests.append(kwargs)
        return batches.pop(0)

    monkeypatch.setattr(backend, "fetch", fetch)
    service = Service(backend, Settings(allowed_chat_ids=["1"], rpc_pages_per_call=1))
    first = await service.get_history(chat_id="1", topic_id=10, limit=1)
    assert message_ids(first) == [23] and first.next_cursor
    assert first.issues == [issue] and not first.scope.complete
    buffered = await service.get_history(
        chat_id="1", topic_id=10, limit=1, cursor=first.next_cursor
    )
    assert message_ids(buffered) == [22] and buffered.next_cursor
    assert buffered.issues == [issue] and len(requests) == 1
    final = await service.get_history(chat_id="1", topic_id=10, cursor=buffered.next_cursor)
    assert message_ids(final) == [21] and final.next_cursor is None
    assert final.issues == [issue] and not final.scope.complete
    assert requests[1]["offset"] == 22 and requests[1]["topic_id"] == 10


@pytest.mark.parametrize(
    ("code", "delay"),
    [("CONNECTION_ERROR", None), ("TELEGRAM_UNAVAILABLE", None), ("RATE_LIMITED", 12)],
)
@pytest.mark.parametrize("order", ["newest_first", "oldest_first"])
async def test_topic_fetch_failure_resumes_same_offset_without_loss_or_reordering(
    code: str, delay: int | None, order: Order, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr("telegram_mcp.service.time.monotonic", lambda: clock[0])
    backend = forum_backend([msg("1", mid, topic_id=1) for mid in range(1, 71)])
    service = Service(backend, Settings(allowed_chat_ids=["1"], rpc_pages_per_call=1))

    async def read(cursor: str | None = None) -> Page:
        return await service.get_history(
            chat_id="1", topic_id=1, order=order, limit=30, cursor=cursor
        )

    first = await read()
    assert len(first.messages) == 30 and first.next_cursor
    backend.faults["1"] = ToolError(code, "Temporary topic read failure.", retry_after=delay)
    failed = await read(first.next_cursor)
    assert failed.messages == [] and failed.next_cursor and not failed.scope.complete
    assert [issue.code for issue in failed.issues] == [code]
    failed_offset = backend.fetch_offsets[-1]
    assert len(backend.fetches) == 2
    backend.faults.clear()
    if delay:
        clock[0] += 2
        pending = await read(failed.next_cursor)
        assert pending.messages == [] and pending.next_cursor
        assert pending.issues[0].retry_after_seconds == 10
        assert len(backend.fetches) == 2
        clock[0] += 10
    page = await read(failed.next_cursor)
    assert backend.fetch_offsets[-1] == failed_offset
    assert page.issues == []
    found = message_ids(first) + message_ids(page)
    while page.next_cursor:
        page = await read(page.next_cursor)
        found.extend(message_ids(page))
    expected = list(range(1, 71))
    assert found == (expected if order == "oldest_first" else expected[::-1])
    assert page.scope.complete and page.issues == []
    assert set(backend.fetch_topic_ids) == {1}


async def test_topic_pins_keep_filter_after_floodwait(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("telegram_mcp.service.time.monotonic", lambda: clock[0])
    backend = forum_backend(
        [msg("1", 21, topic_id=1), msg("1", 22, topic_id=10), msg("1", 23, topic_id=1)]
    )
    backend.pinned_message_keys = {"1:21", "1:22"}
    backend.faults["1"] = ToolError("RATE_LIMITED", "Retry later.", retry_after=9)
    service = Service(backend, Settings(allowed_chat_ids=["1"]))
    failed = await service.get_pinned_messages("1", topic_id=1)
    assert failed.messages == [] and failed.next_cursor and failed.topic_id == 1
    backend.faults.clear()
    waiting = await service.get_pinned_messages("1", topic_id=1, cursor=failed.next_cursor)
    assert waiting.messages == [] and len(backend.fetches) == 1
    clock[0] += 9
    recovered = await service.get_pinned_messages("1", topic_id=1, cursor=failed.next_cursor)
    assert message_ids(recovered) == [21]
    assert recovered.scope.complete and recovered.issues == []
    assert backend.fetch_pinned_flags == [True, True]
    assert backend.fetch_topic_ids == [1, 1]
