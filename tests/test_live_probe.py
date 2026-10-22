"""The live harness is tested offline; these tests never open a Telegram session."""

import json
from dataclasses import asdict
from typing import Any, Self
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from pydantic import SecretStr

from scripts.probe_telegram import Check, LiveBackend, main, print_check, probe, validate_selectors
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend
from telegram_mcp.application.ports import Batch, MessageOffset, TopicBatch, TopicOffset
from telegram_mcp.application.requests import HistoryRead, MessageRead, SearchRead
from telegram_mcp.domain.models import Chat, ForumTopic, MessageRef, ToolError
from tests.fakes import FakeBackend, msg

SECRET = "private-secret-text"


class ProbeFake(FakeBackend):
    def __init__(self) -> None:
        chats = [
            Chat(id="1", username="selected_one", title=SECRET, type="private"),
            Chat(id="-1002", username="selected_two", title=SECRET, type="channel"),
            Chat(id="99", username="outside", title=SECRET, type="private"),
        ]
        messages = [msg(chat.id, mid, f"link {SECRET}") for chat in chats for mid in range(1, 16)]
        for message in messages:
            message.has_link = True
            if message.chat.id == "-1002" and 4 <= message.message_ref.message_id <= 6:
                message.album_id = "1234"
        messages[0].reply_to = MessageRef(chat_id="99", message_id=1)
        super().__init__(messages, chats)
        self.selected: set[str] = set()
        self.connected = True
        self.reconnects = 0
        self.disconnects = 0
        self.requests: list[tuple[str, MessageRead, MessageOffset, int]] = []
        self.settings: Settings | None = None

    def factory(self, settings: Settings) -> Self:
        self.settings = settings
        return self

    def select(self, chat_ids: set[str]) -> None:
        self.selected = set(chat_ids)

    async def disconnect_owned(self) -> None:
        assert not self.closed
        self.backend_calls.append("disconnect_owned")
        self.connected = False
        self.disconnects += 1

    async def status(self) -> dict[str, object]:
        if not self.connected:
            assert not self.closed
            self.reconnects += 1
            self.connected = True
        return await super().status()

    async def catalog(self) -> Any:
        raise AssertionError("The probe must not enumerate dialogs.")

    async def read(
        self, chat_id: str, request: MessageRead, *, offset: MessageOffset = 0, limit: int = 30
    ) -> Batch:
        assert chat_id in self.selected
        assert 1 <= limit <= 5
        self.requests.append((chat_id, request, offset, limit))
        return await super().read(chat_id, request, offset=offset, limit=limit)


async def test_probe_scope_bounds_redaction_and_reconnect() -> None:
    backend = ProbeFake()
    settings = Settings(
        api_id=12345,
        api_hash=SecretStr(SECRET),
        allowed_chat_ids=["99"],
        allowed_folder_ids=[3],
        allow_all_chats=True,
        allow_public_channel_reads=True,
        allow_public_channel_discovery=True,
    )
    original = settings.model_dump()
    emitted: list[Check] = []
    checks = await probe(
        settings,
        chats=["@selected_one", "https://t.me/selected_two"],
        message_refs=["1:1", "https://t.me/selected_two/5"],
        backend_factory=backend.factory,
        emit=emitted.append,
    )
    assert checks == emitted
    assert not any(check.issue_codes for check in checks)
    assert settings.model_dump() == original
    assert backend.settings is not None and backend.settings is not settings
    assert backend.settings.allowed_chat_ids == ["1", "-1002"]
    assert not backend.settings.allowed_folder_ids
    assert not backend.settings.allow_all_chats
    assert not backend.settings.allow_public_channel_reads
    assert not backend.settings.allow_public_channel_discovery
    assert backend.settings.rpc_pages_per_call == 1
    assert backend.selected == {"1", "-1002"}
    assert set(backend.fetches) == backend.selected
    assert all(cid in backend.selected and len(ids) <= 3 for cid, ids in backend.batch_reads)
    assert all(key.split(":")[0] in backend.selected for key in backend.reads)
    assert backend.history_windows == [("-1002", 5, 5, False), ("-1002", 5, 5, True)]
    assert not backend.topic_fetches
    assert not backend.global_fetches
    assert not backend.discovery_search_calls
    assert not backend.discovery_similar_calls
    assert not backend.thread_calls
    assert backend.public_checks == []
    for index in range(2):
        assert [c.count for c in checks if c.check.startswith(f"history_{index}_")] == [5, 5]
        assert len([c for c in checks if c.check.startswith(f"links_{index}_")]) == 2
    searches = [request for _, request, _, _ in backend.requests if isinstance(request, SearchRead)]
    assert len(searches) == 12
    assert (
        sum(request.query == "" and request.filters.has_link is True for request in searches) == 4
    )
    assert {request.filters.content_type for request in searches} == {
        None,
        "photo",
        "voice",
        "audio",
    }
    assert backend.disconnects == backend.reconnects == 1
    disconnected = backend.backend_calls.index("disconnect_owned")
    assert backend.backend_calls[disconnected + 1] == "status"
    assert "fetch" in backend.backend_calls[disconnected + 2 : -1]
    assert backend.backend_calls[-1] == "close"
    assert backend.closed
    output = json.dumps([asdict(check) for check in checks])
    for private in (SECRET, "selected_one", "selected_two", "1:1", "-1002:5"):
        assert private not in output


async def test_foreign_message_is_rejected_before_any_content() -> None:
    backend = ProbeFake()
    checks = await probe(
        Settings(),
        chats=["@selected_one"],
        message_refs=["https://t.me/outside/1"],
        backend_factory=backend.factory,
    )
    assert checks == [Check("probe", issue_codes=["VALIDATION_SCOPE_DENIED"])]
    assert not backend.requests and not backend.reads and not backend.batch_reads
    assert not backend.history_windows and not backend.topic_fetches
    assert backend.disconnects == 0 and backend.closed


@pytest.mark.parametrize(
    ("chats", "refs", "query"),
    [
        ([], [], "link"),
        (["a"] * 4, [], "link"),
        (["a"], ["1:1"] * 4, "link"),
        (["a"], [], ""),
        (["a"], [], "a" * 65),
        ([" "], [], "link"),
    ],
)
async def test_invalid_bounds_fail_before_backend_creation(
    chats: list[str], refs: list[str], query: str
) -> None:
    def forbidden(settings: Settings) -> Any:
        raise AssertionError("Invalid arguments must not create a backend.")

    with pytest.raises(ToolError) as caught:
        await probe(
            Settings(), chats=chats, message_refs=refs, query=query, backend_factory=forbidden
        )
    assert caught.value.code == "INVALID_ARGUMENT"


def test_discussion_links_are_rejected_before_resolution() -> None:
    with pytest.raises(ToolError) as caught:
        validate_selectors(["@selected_one"], ["https://t.me/selected_one/1?comment=9"], "link")
    assert caught.value.code == "VALIDATION_SCOPE_DENIED"


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "SESSION_IN_USE"])
async def test_temporary_failure_stops_without_retry_or_disconnect(code: str) -> None:
    backend = ProbeFake()
    backend.faults["1"] = ToolError(code, SECRET, 60)
    checks = await probe(Settings(), chats=["1"], message_refs=[], backend_factory=backend.factory)
    assert checks[-1].issue_codes == [code]
    assert len(backend.requests) == 1
    assert backend.disconnects == 0
    assert backend.closed
    assert SECRET not in json.dumps([asdict(check) for check in checks])


async def test_session_lease_conflict_stops_during_resolution() -> None:
    backend = ProbeFake()

    async def conflict(ref: str, *, refresh: bool = False) -> Chat:
        raise ToolError("SESSION_IN_USE", SECRET)

    with patch.object(backend, "resolve_chat", conflict):
        checks = await probe(
            Settings(), chats=["1"], message_refs=[], backend_factory=backend.factory
        )
    assert checks == [Check("probe", issue_codes=["SESSION_IN_USE"])]
    assert not backend.requests and backend.disconnects == 0 and backend.closed


async def test_unknown_error_is_redacted() -> None:
    backend = ProbeFake()

    async def broken(ref: str, *, refresh: bool = False) -> Chat:
        raise RuntimeError(SECRET)

    with patch.object(backend, "resolve_chat", broken):
        checks = await probe(
            Settings(), chats=["1"], message_refs=[], backend_factory=backend.factory
        )
    assert checks == [Check("probe", issue_codes=["UNEXPECTED_ERROR"])]
    assert backend.closed


async def test_optional_topics_only_read_selected_forum() -> None:
    backend = ProbeFake()
    backend.chats[0].type = "group"
    backend.chats[0].is_supergroup = True
    backend.chats[0].is_forum = True
    topic = ForumTopic(id=1, chat_id="1", title=SECRET)
    backend.topics[("1", 1)] = topic
    backend.topic_batches[("1", TopicOffset())] = TopicBatch(
        [topic], None, True, False, telegram_total_count=1
    )
    checks = await probe(
        Settings(),
        chats=["1", "-1002"],
        message_refs=[],
        topics=True,
        backend_factory=backend.factory,
    )
    assert not any(check.issue_codes for check in checks)
    assert backend.topic_fetches == [("1", TopicOffset(), 3, None)]
    assert [check.check for check in checks if check.check.startswith("topic")] == [
        "topics_0",
        "topic_history_0",
    ]


async def test_live_backend_guards_run_before_telegram_calls() -> None:
    backend = LiveBackend(Settings())
    backend.select({"1"})
    operations = [
        backend.catalog(),
        backend.search_global(),
        backend.search_public_channels(),
        backend.get_similar_channels(),
        backend.message(MessageRef(chat_id="99", message_id=1)),
        backend.messages("1", list(range(1, 7))),
        backend.read("1", HistoryRead(), limit=6),
        backend.history_window("99", anchor_id=1, limit=5, newer=False),
        backend.fetch_topics("99", limit=3),
        backend.get_topic("99", 1),
        backend.discussion_chat(Chat(id="1", title=SECRET, type="group")),
        backend.thread_root(MessageRef(chat_id="1", message_id=1), "99"),
        backend.disconnect_owned(),
    ]
    for operation in operations:
        with pytest.raises(ToolError) as caught:
            await operation
        assert caught.value.code == "VALIDATION_SCOPE_DENIED"
    assert backend.runtime.client is None
    assert not backend.runtime.lease.held


async def test_disconnect_only_owned_client_keeps_runtime_and_lease() -> None:
    backend = LiveBackend(Settings())
    client = AsyncMock()
    backend.runtime.client = client
    runtime = backend.runtime
    lease = runtime.lease
    with patch.object(type(lease), "held", new_callable=PropertyMock, return_value=True):
        await backend.disconnect_owned()
        assert runtime.lease.held
    assert backend.runtime is runtime and runtime.client is client and runtime.lease is lease
    client.disconnect.assert_awaited_once_with()
    with pytest.raises(ToolError) as caught:
        await backend.disconnect_owned()
    assert caught.value.code == "VALIDATION_SCOPE_DENIED"
    assert client.disconnect.await_count == 1


async def test_live_backend_latches_transport_failure_before_next_read() -> None:
    backend = LiveBackend(Settings())
    backend.select({"1"})
    failure = ToolError("RATE_LIMITED", SECRET, 60)
    with patch.object(TelegramBackend, "read", new=AsyncMock(side_effect=failure)) as read:
        for oldest_first in (False, True):
            with pytest.raises(ToolError) as caught:
                await backend.read("1", HistoryRead(), offset=int(oldest_first), limit=2)
            assert caught.value.code == "RATE_LIMITED"
        assert read.await_count == 1
    assert backend.runtime.client is None


def test_cli_rejects_global_flag_and_redacts_invalid_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--chat", "@selected_one", "--global", SECRET]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "check": "setup",
        "count": 0,
        "issue_codes": ["INVALID_ARGUMENT"],
    }
    assert captured.err == ""
    assert SECRET not in captured.out


def test_output_contains_only_check_count_codes(capsys: pytest.CaptureFixture[str]) -> None:
    print_check(Check("history_0_0", 5, ["SEARCH_INEXACT"]))
    assert json.loads(capsys.readouterr().out) == {
        "check": "history_0_0",
        "count": 5,
        "issue_codes": ["SEARCH_INEXACT"],
    }
