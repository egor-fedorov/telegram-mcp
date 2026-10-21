from typing import Any

import pytest

from telegram_mcp.backend import HistoryWindow
from telegram_mcp.config import Settings
from telegram_mcp.models import (
    Attachment,
    Chat,
    Entity,
    Folder,
    FormattedText,
    ForwardOrigin,
    Issue,
    Message,
    MessageRef,
    PollData,
    ReactionCount,
    ReactionSummary,
    ToolError,
)
from telegram_mcp.service import Service
from tests.fakes import BASE, FakeBackend, msg

CHAT_ID = "-1001"
ALBUM_ID = "9007199254740993"


def member(mid: int, *, album_id: str | None = ALBUM_ID, text: str = "caption") -> Message:
    message = msg(CHAT_ID, mid, text, content="photo")
    message.album_id = album_id
    return message


class AlbumBackend(FakeBackend):
    def __init__(self, messages: list[Message] | None = None, **kwargs: Any):
        super().__init__(messages, **kwargs)
        self.window_calls: list[tuple[str, dict[str, Any]]] = []
        self.windows: dict[bool, HistoryWindow | ToolError] = {}
        self.anchor_override: Message | None = None

    async def message(self, ref: MessageRef) -> Message | None:
        result = await super().message(ref)
        return self.anchor_override if self.anchor_override is not None else result

    async def history_window(self, chat_id: str, **kwargs: Any) -> HistoryWindow:
        self.window_calls.append((chat_id, kwargs))
        window = self.windows.get(kwargs.get("newer", False))
        if isinstance(window, ToolError):
            raise window
        if window is not None:
            return window
        return await super().history_window(chat_id, **kwargs)


def album_service(backend: AlbumBackend, **settings: Any) -> Service:
    return Service(backend, Settings(**(settings or {"allow_all_chats": True})))


@pytest.mark.parametrize("scan_limit", [0, -1, 101, True, False, 1.0, "30", None])
async def test_invalid_album_scan_limit_does_not_contact_backend(scan_limit: Any) -> None:
    backend = AlbumBackend([member(20)])
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await album_service(backend).get_album(f"{CHAT_ID}:20", scan_limit=scan_limit)
    assert backend.backend_calls == [] and backend.window_calls == []


@pytest.mark.parametrize("ref", ["invalid", "https://t.me/source/not-a-number"])
async def test_invalid_album_reference_does_not_read_messages(ref: str) -> None:
    backend = AlbumBackend([member(20)])
    with pytest.raises(ToolError):
        await album_service(backend).get_album(ref)
    assert backend.reads == [] and backend.window_calls == []


@pytest.mark.parametrize("ref_kind", ["key", "object", "link"])
async def test_album_reference_forms_resolve_to_same_canonical_source(ref_kind: str) -> None:
    chat = Chat(id=CHAT_ID, title="Source", type="channel", username="source")
    backend = AlbumBackend([member(19), member(20), member(21)], chats=[chat])
    ref: str | MessageRef = {
        "key": f"{CHAT_ID}:20",
        "object": MessageRef(chat_id=CHAT_ID, message_id=20),
        "link": "https://t.me/source/20",
    }[ref_kind]
    result = await album_service(backend).get_album(ref)
    assert result.anchor_ref == MessageRef(chat_id=CHAT_ID, message_id=20)
    assert result.album_id == ALBUM_ID
    assert [m.message_ref.message_id for m in result.messages] == [19, 20, 21]
    assert result.order == "oldest_first" and result.completeness == "unknown"
    assert result.scan_limit == 30
    assert (result.scanned_before, result.scanned_after) == (1, 1)
    assert result.before_exhausted is True and result.after_exhausted is True
    assert backend.backend_calls[:2] == ["catalog", "resolve_message"]
    assert backend.reads == [f"{CHAT_ID}:20"]
    assert backend.window_calls == [
        (CHAT_ID, {"anchor_id": 20, "limit": 30, "newer": False}),
        (CHAT_ID, {"anchor_id": 20, "limit": 30, "newer": True}),
    ]


@pytest.mark.parametrize("public_mode", [False, True])
async def test_unallowed_group_album_is_denied_before_anchor_read(public_mode: bool) -> None:
    backend = AlbumBackend([member(20)])
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await album_service(backend, allow_public_channel_reads=public_mode).get_album(
            f"{CHAT_ID}:20"
        )
    assert backend.reads == [] and backend.window_calls == []


async def test_explicit_public_album_grant_is_rechecked_and_does_not_follow_links() -> None:
    chat = Chat(id=CHAT_ID, title="Public", type="channel", username="source")
    other_ref = MessageRef(chat_id="-1002", message_id=50)
    anchor = member(20)
    anchor.reply_to = other_ref
    anchor.thread_ref = other_ref
    anchor.forward_origin = ForwardOrigin(message_ref=other_ref, saved_from_ref=other_ref)
    backend = AlbumBackend([anchor, member(21)], chats=[], external_chats=[chat])
    service = album_service(backend, allow_public_channel_reads=True)
    result = await service.get_album("https://t.me/source/20")
    assert len(result.messages) == 2
    assert backend.public_checks == [CHAT_ID]
    assert backend.reads == [f"{CHAT_ID}:20"]
    assert len(backend.history_windows) == 2 and backend.thread_calls == 0
    assert all(name not in backend.backend_calls for name in ("discussion_chat", "thread_root"))
    chat.username = None
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_album(f"{CHAT_ID}:20")
    assert backend.public_checks == [CHAT_ID, CHAT_ID]
    assert backend.reads == [f"{CHAT_ID}:20"]
    assert len(backend.window_calls) == 2
    assert service.settings.allowed_chat_ids == [] and backend.chats == []


async def test_public_album_is_denied_when_public_mode_is_disabled() -> None:
    chat = Chat(id=CHAT_ID, title="Public", type="channel", username="source")
    backend = AlbumBackend([member(20)], chats=[], external_chats=[chat])
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await album_service(backend, allow_public_channel_reads=False).get_album(
            "https://t.me/source/20"
        )
    assert backend.public_checks == [] and backend.reads == [] and backend.window_calls == []


async def test_album_folder_permission_revocation_uses_fresh_catalog() -> None:
    folder = Folder(id=4, title="Allowed", position=0, included_chat_ids=[CHAT_ID])
    backend = AlbumBackend([member(20)], folders=[folder])
    service = album_service(backend, allowed_folder_ids=[4])
    await service.get_album(f"{CHAT_ID}:20")
    folder.included_chat_ids = []
    with pytest.raises(ToolError, match="ACCESS_DENIED"):
        await service.get_album(f"{CHAT_ID}:20")
    assert backend.backend_calls.count("catalog") == 2
    assert backend.reads == [f"{CHAT_ID}:20"] and len(backend.window_calls) == 2


@pytest.mark.parametrize("failure", ["catalog", "public"])
async def test_album_access_check_failure_cannot_release_anchor(failure: str) -> None:
    chat = Chat(id=CHAT_ID, title="Public", type="channel", username="source")
    backend = AlbumBackend([member(20)], chats=[], external_chats=[chat])
    error = ToolError("CONNECTION_ERROR", "Temporary failure")
    if failure == "catalog":
        backend.catalog_fault = error
    else:
        backend.public_faults[CHAT_ID] = error
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await album_service(backend, allow_public_channel_reads=True).get_album(f"{CHAT_ID}:20")
    assert backend.reads == [] and backend.window_calls == []


@pytest.mark.parametrize("anchor_state", ["missing", "other_id", "other_ref_chat", "other_chat"])
async def test_unavailable_or_mismatched_anchor_cannot_start_album_scan(anchor_state: str) -> None:
    backend = AlbumBackend([member(20)])
    if anchor_state == "missing":
        backend.stored_messages = []
    else:
        wrong = member(20)
        if anchor_state == "other_id":
            wrong.message_ref = MessageRef(chat_id=CHAT_ID, message_id=21)
        elif anchor_state == "other_ref_chat":
            wrong.message_ref = MessageRef(chat_id="-1002", message_id=20)
        else:
            wrong.chat = Chat(id="-1002", title="Foreign", type="channel")
        backend.anchor_override = wrong
    with pytest.raises(ToolError, match="MESSAGE_UNAVAILABLE"):
        await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert backend.window_calls == []


async def test_nongrouped_anchor_reports_not_an_album_without_scan() -> None:
    backend = AlbumBackend([member(20, album_id=None)])
    with pytest.raises(ToolError, match="NOT_AN_ALBUM"):
        await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert backend.window_calls == []


async def test_album_anchor_read_error_is_not_converted_to_missing_message() -> None:
    backend = AlbumBackend([member(20)])
    backend.message_faults[CHAT_ID] = ToolError("RATE_LIMITED", "Wait", 11)
    with pytest.raises(ToolError, match="RATE_LIMITED") as caught:
        await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert caught.value.retry_after == 11 and backend.window_calls == []


async def test_album_scan_crosses_interleaved_messages_and_sparse_ids_without_ten_item_cap() -> (
    None
):
    before = [member(mid * 100) for mid in range(1, 8)]
    after = [member(mid * 100) for mid in range(9, 16)]
    unrelated = [member(701, album_id=None), member(901, album_id="another")]
    anchor = member(800)
    before[0].sent_at = BASE.replace(year=2030)
    after[-1].sent_at = BASE.replace(year=2020)
    backend = AlbumBackend([*before, anchor, *after, *unrelated])
    result = await album_service(backend).get_album(f"{CHAT_ID}:800", scan_limit=10)
    assert [m.message_ref.message_id for m in result.messages] == list(range(100, 1600, 100))
    assert (result.scanned_before, result.scanned_after) == (8, 8)
    assert result.before_exhausted is True and result.after_exhausted is True
    assert result.completeness == "unknown"
    assert len(backend.window_calls) == 2


async def test_album_filtering_rejects_foreign_peers_wrong_direction_and_duplicate_anchor() -> None:
    anchor = member(20, text="authoritative anchor")
    foreign_ref = member(17, text="foreign reference")
    foreign_ref.message_ref = MessageRef(chat_id="-1002", message_id=17)
    foreign_chat = member(18, text="foreign chat")
    foreign_chat.chat = Chat(id="-1002", title="Foreign", type="channel")
    backend = AlbumBackend([anchor])
    backend.windows = {
        False: HistoryWindow(
            [
                member(19, text="first duplicate"),
                member(19, text="second duplicate"),
                member(20, text="replaced anchor"),
                member(21, text="wrong direction"),
                member(16, album_id="another"),
                foreign_ref,
                foreign_chat,
            ],
            7,
            True,
        ),
        True: HistoryWindow(
            [member(21), member(19), member(20), member(22, album_id=None)], 4, True
        ),
    }
    result = await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert [m.message_ref.message_id for m in result.messages] == [19, 20, 21]
    assert result.messages[0].text == "first duplicate"
    assert result.messages[1].text == "authoritative anchor"
    assert (result.scanned_before, result.scanned_after) == (7, 4)


@pytest.mark.parametrize("scan_limit", [1, 30, 100])
async def test_album_scans_bound_rows_in_each_direction_and_never_claim_completeness(
    scan_limit: int,
) -> None:
    backend = AlbumBackend([member(mid) for mid in range(1, 302)])
    result = await album_service(backend).get_album(f"{CHAT_ID}:151", scan_limit=scan_limit)
    assert [m.message_ref.message_id for m in result.messages] == list(
        range(151 - scan_limit, 152 + scan_limit)
    )
    assert len(result.messages) == 1 + 2 * scan_limit
    assert result.scanned_before == result.scanned_after == scan_limit
    assert result.before_exhausted is False and result.after_exhausted is False
    assert result.completeness == "unknown"
    assert "next_cursor" not in result.model_dump()
    assert len(backend.window_calls) == 2


async def test_empty_successful_album_windows_report_zero_and_exhausted() -> None:
    backend = AlbumBackend([member(20)])
    result = await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert [m.message_ref.message_id for m in result.messages] == [20]
    assert result.scanned_before == result.scanned_after == 0
    assert result.before_exhausted is True and result.after_exhausted is True
    assert result.completeness == "unknown" and result.issues == []


async def test_album_windows_preserve_batch_issues_and_do_not_fetch_additional_pages() -> None:
    backend = AlbumBackend([member(20)])
    issue = Issue(code="MESSAGE_UNAVAILABLE", message="A neighbor was unavailable", chat_id=CHAT_ID)
    backend.windows = {
        False: HistoryWindow([], 3, False, issues=[issue]),
        True: HistoryWindow([member(21)], 2, False, issues=[issue]),
    }
    result = await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert [m.message_ref.message_id for m in result.messages] == [20, 21]
    assert result.scanned_before == 3 and result.scanned_after == 2
    assert result.before_exhausted is False and result.after_exhausted is False
    assert result.issues == [issue, issue]
    assert len(backend.window_calls) == 2


async def test_retryable_issue_with_partial_history_window_stops_remaining_requests() -> None:
    backend = AlbumBackend([member(20)])
    issue = Issue(code="RATE_LIMITED", message="Wait", chat_id=CHAT_ID, retry_after_seconds=9)
    backend.windows[False] = HistoryWindow([member(19)], 2, False, issues=[issue])
    result = await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert [m.message_ref.message_id for m in result.messages] == [19, 20]
    assert result.scanned_before == 2 and result.before_exhausted is False
    assert result.scanned_after == 0 and result.after_exhausted is None
    assert result.issues == [issue] and len(backend.window_calls) == 1


async def test_album_unavailable_slots_consume_scan_budget_without_fabricating_members() -> None:
    backend = AlbumBackend([member(20)])
    issue = Issue(code="MESSAGE_UNAVAILABLE", message="Unavailable message", chat_id=CHAT_ID)
    backend.windows[False] = HistoryWindow([], 5, False, issues=[issue])
    backend.windows[True] = HistoryWindow([member(21)], 3, True)
    result = await album_service(backend).get_album(f"{CHAT_ID}:20", scan_limit=5)
    assert [m.message_ref.message_id for m in result.messages] == [20, 21]
    assert result.scanned_before == 5 and result.scanned_after == 3
    assert result.before_exhausted is False and result.after_exhausted is True
    assert result.issues == [issue]
    assert len(backend.window_calls) == 2


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"])
@pytest.mark.parametrize("direction", ["before", "after"])
async def test_retryable_album_scan_failure_returns_partial_without_subsequent_calls(
    code: str,
    direction: str,
) -> None:
    backend = AlbumBackend([member(19), member(20), member(21)])
    backend.windows[direction == "after"] = ToolError(
        code, "Temporary failure", 12 if code == "RATE_LIMITED" else None
    )
    result = await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert [m.message_ref.message_id for m in result.messages] == (
        [20] if direction == "before" else [19, 20]
    )
    assert len(backend.window_calls) == (1 if direction == "before" else 2)
    assert result.scanned_after == 0 and result.after_exhausted is None
    assert result.scanned_before == (0 if direction == "before" else 1)
    assert result.before_exhausted is (None if direction == "before" else True)
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.code == code and issue.chat_id == CHAT_ID
    assert issue.message_ref == result.anchor_ref
    assert issue.retry_after_seconds == (12 if code == "RATE_LIMITED" else None)
    assert result.completeness == "unknown"


@pytest.mark.parametrize("direction", ["before", "after"])
async def test_permanent_album_scan_failure_keeps_other_direction(direction: str) -> None:
    backend = AlbumBackend([member(19), member(20), member(21)])
    backend.windows[direction == "after"] = ToolError("MESSAGE_UNAVAILABLE", "Unavailable")
    result = await album_service(backend).get_album(f"{CHAT_ID}:20")
    assert [m.message_ref.message_id for m in result.messages] == (
        [20, 21] if direction == "before" else [19, 20]
    )
    assert len(backend.window_calls) == 2
    assert getattr(result, f"{direction}_exhausted") is None
    assert getattr(result, f"scanned_{direction}") == 0
    assert result.issues[0].code == "MESSAGE_UNAVAILABLE"


async def test_retrying_album_after_transient_failure_starts_fresh_without_losing_members() -> None:
    backend = AlbumBackend([member(19), member(20), member(21)])
    service = album_service(backend)
    backend.windows[False] = ToolError("CONNECTION_ERROR", "Disconnected")
    first = await service.get_album(f"{CHAT_ID}:20")
    assert len(first.messages) == 1 and first.issues
    backend.windows.clear()
    second = await service.get_album(f"{CHAT_ID}:20")
    assert [m.message_ref.message_id for m in second.messages] == [19, 20, 21]
    assert second.issues == []
    assert backend.reads == [f"{CHAT_ID}:20", f"{CHAT_ID}:20"]
    assert backend.backend_calls.count("catalog") == 2


@pytest.mark.parametrize("count", [1, 3, 11, 201])
async def test_album_unicode_budget_and_continuation_preserve_all_other_metadata(
    count: int,
) -> None:
    messages = [member(mid, text="😀A" * 7000) for mid in range(1, count + 1)]
    for message in messages:
        message.entities = [Entity(type="MessageEntityBold", offset=0, length=3)]
        message.attachments = [Attachment(id="photo:77", type="photo")]
        message.topic_id = 7
        message.forward_origin = ForwardOrigin(name="Hidden source", sent_at=BASE)
        message.view_count = 0
        message.reactions = ReactionSummary(results=[ReactionCount(type="paid", count=10)])
        message.poll = PollData(
            id="9007199254740994", question=FormattedText(text="Why?"), answers=[]
        )
        message.url = f"https://t.me/source/{message.message_ref.message_id}"
    originals = [message.model_dump() for message in messages]
    backend = AlbumBackend(messages)
    service = album_service(backend)
    result = await service.get_album(f"{CHAT_ID}:{1 + count // 2}", scan_limit=100)
    assert len(result.messages) == count
    cap = min(12000, 100000 // count)
    assert sum(len(message.text) for message in result.messages) <= 100000
    for actual, original in zip(result.messages, messages, strict=True):
        assert actual.text == original.text[:cap]
        assert actual.text_offset == 0 and actual.next_text_offset == cap
        assert actual.text_total_length == len(original.text)
        clipped_fields = {"text", "text_offset", "text_total_length", "next_text_offset"}
        assert actual.model_dump(exclude=clipped_fields) == original.model_dump(
            exclude=clipped_fields
        )
    assert [message.model_dump() for message in messages] == originals
    first = result.messages[0]
    continuation = await service.get_messages(
        [first.message_ref], text_offset=cap, text_limit=100000
    )
    assert first.text + continuation.messages[0].text == messages[0].text
