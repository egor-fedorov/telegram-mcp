import json

import pytest
from mcp.client import Client

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service as Service
from telegram_mcp.domain.models import (
    Attachment,
    Chat,
    ForwardOrigin,
    Message,
    MessageRef,
    ToolError,
)
from tests.support.fakes import BASE, FakeBackend, msg
from tests.support.message_metadata import assert_metadata, rich_message


def album_message(mid: int, text: str = "A😀B album caption") -> Message:
    message = rich_message(mid, text)
    message.album_id = "9007199254740997"
    message.attachments = [Attachment(id=str(mid), type="photo", mime_type="image/jpeg")]
    message.forward_origin = ForwardOrigin(
        name="Original author", saved_from_ref=MessageRef(chat_id="-1009", message_id=1)
    )
    message.url = f"https://t.me/example/{mid}"
    message.edited_at = BASE
    return message


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_album_mcp_schema_exposes_bounded_read_only_result(mode: str) -> None:
    backend = FakeBackend()
    async with Client(
        create_server(Service(backend, Settings()), schema_mode="full"), mode=mode
    ) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == 22
        tool = next(tool for tool in tools if tool.name == "get_album")
        assert tool.annotations is not None and tool.annotations.read_only_hint
        assert tool.input_schema["required"] == ["ref"]
        assert set(tool.input_schema["properties"]) == {"ref", "scan_limit"}
        scan = tool.input_schema["properties"]["scan_limit"]
        assert (scan["type"], scan["minimum"], scan["maximum"], scan["default"]) == (
            "integer",
            1,
            100,
            30,
        )
        assert tool.output_schema is not None
        properties = tool.output_schema["properties"]
        assert properties["completeness"]["const"] == "unknown"
        assert properties["order"]["const"] == "oldest_first"
        assert "next_cursor" not in properties and "scope" not in properties
        assert {"type": "null"} in properties["before_exhausted"]["anyOf"]
        assert {"type": "null"} in properties["after_exhausted"]["anyOf"]
        for arguments in (
            {},
            {"ref": []},
            {"ref": {}},
            {"ref": {"chat_id": "-1001", "message_id": 0}},
            *({"ref": "-1001:20", "scan_limit": value} for value in (0, 101, True, "2", 2.0, None)),
        ):
            result = await client.call_tool("get_album", arguments)
            assert result.is_error, arguments
        assert backend.backend_calls == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    "reference",
    ["-1001:20", "https://t.me/example/20", {"chat_id": "-1001", "message_id": 20}],
)
async def test_album_mcp_reference_dispatch_preserves_members_and_metadata(
    mode: str, reference: str | dict[str, object]
) -> None:
    members = [album_message(mid) for mid in (18, 20, 22)]
    chat = Chat(id="-1001", title="Example", type="channel", username="example")
    for member in members:
        member.chat = chat
    backend = FakeBackend([*members, msg("-1001", 19), msg("-1001", 21)], chats=[chat])
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=[chat.id])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        result = await client.call_tool("get_album", {"ref": reference})
        assert not result.is_error and result.structured_content is not None
        data = result.structured_content
        assert data["anchor_ref"] == f"{chat.id}:20"
        assert data["album_id"] == members[1].album_id
        assert data["order"] == "oldest_first" and data["completeness"] == "unknown"
        assert data["scan_limit"] == 30
        assert data["scanned_before"] == data["scanned_after"] == 2
        assert data["before_exhausted"] is data["after_exhausted"] is True
        assert data["issues"] == []
        assert [int(item["message_ref"].split(":")[1]) for item in data["messages"]] == [18, 20, 22]
        for item, original in zip(data["messages"], members, strict=True):
            assert_metadata(item, original)
            serialized = original.model_dump(mode="json", exclude_none=True)
            for field in ("text", "attachments", "sent_at", "edited_at", "url"):
                assert item[field] == serialized[field]
            assert item["forward_origin"] == {
                "name": "Original author",
                "saved_from_ref": "-1009:1",
            }
        assert backend.reads == ["-1001:20"]
        assert backend.history_windows == [("-1001", 20, 30, False), ("-1001", 20, 30, True)]
        assert backend.fetches == []
        assert backend.batch_reads == [] and backend.thread_calls == 0
        assert "discussion_chat" not in backend.backend_calls


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_album_mcp_text_budget_keeps_metadata_and_unicode_continuation(mode: str) -> None:
    members = [album_message(mid, "A😀B" * 6000) for mid in range(1, 12)]
    original = [member.model_dump(mode="json") for member in members]
    backend = FakeBackend(members)
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        result = await client.call_tool("get_album", {"ref": "-1001:6", "scan_limit": 5})
        assert not result.is_error and result.structured_content is not None
        messages = result.structured_content["messages"]
        assert len(messages) == 11
        assert sum(len(message["text"]) for message in messages) <= 100000
        for message, member in zip(messages, members, strict=True):
            offset = message["next_text_offset"]
            assert 0 < offset <= 12000
            assert message["text"] == member.text[:offset]
            assert message["text_total_length"] == len(member.text)
            assert_metadata(message, member)
        first = messages[0]
        continuation = await client.call_tool(
            "get_messages",
            {"refs": [first["message_ref"]], "text_offset": first["next_text_offset"]},
        )
        assert not continuation.is_error and continuation.structured_content is not None
        tail = continuation.structured_content["messages"][0]
        assert first["text"] + tail["text"] == members[0].text
        assert tail["next_text_offset"] is None
        assert_metadata(tail, members[0])
        assert [member.model_dump(mode="json") for member in members] == original


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE"])
async def test_album_mcp_temporary_neighbor_failure_returns_partial_result(
    mode: str, code: str
) -> None:
    anchor = album_message(20)
    backend = FakeBackend([anchor])
    backend.faults["-1001"] = ToolError(
        code, "Neighbor retrieval failed.", 37 if code == "RATE_LIMITED" else None
    )
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        result = await client.call_tool("get_album", {"ref": "-1001:20"})
        assert not result.is_error and result.structured_content is not None
        data = result.structured_content
        assert len(data["messages"]) == 1
        assert int(data["messages"][0]["message_ref"].split(":")[1]) == 20
        assert data["completeness"] == "unknown" and "next_cursor" not in data
        assert data["before_exhausted"] is data["after_exhausted"] is None
        assert data["scanned_before"] == data["scanned_after"] == 0
        assert len(data["issues"]) == 1
        issue = data["issues"][0]
        assert issue["code"] == code
        assert issue.get("retry_after_seconds") == (37 if code == "RATE_LIMITED" else None)
        assert issue["message_ref"] == anchor.message_ref.key
        assert backend.history_windows == [("-1001", 20, 30, False)]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_album_mcp_retries_whole_call_after_network_failure(mode: str) -> None:
    backend = FakeBackend([album_message(mid) for mid in (19, 20, 21)])
    backend.faults["-1001"] = ToolError("CONNECTION_ERROR", "Connection failed.")
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        first = await client.call_tool("get_album", {"ref": "-1001:20"})
        assert first.structured_content is not None and first.structured_content["issues"]
        backend.faults.clear()
        second = await client.call_tool("get_album", {"ref": "-1001:20"})
        assert not second.is_error and second.structured_content is not None
        assert second.structured_content["issues"] == []
        assert len(second.structured_content["messages"]) == 3
        assert second.structured_content["completeness"] == "unknown"
        assert backend.reads == ["-1001:20", "-1001:20"]
        assert backend.history_windows == [
            ("-1001", 20, 30, False),
            ("-1001", 20, 30, False),
            ("-1001", 20, 30, True),
        ]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_album_mcp_initial_failures_are_errors_without_neighbor_scans(mode: str) -> None:
    for failure, reference in (
        ("INVALID_ARGUMENT", "invalid"),
        ("MESSAGE_UNAVAILABLE", "-1001:99"),
        ("NOT_AN_ALBUM", "-1001:21"),
        ("ACCESS_DENIED", "-1001:20"),
        ("AUTH_REQUIRED", "-1001:20"),
        ("CONNECTION_ERROR", "-1001:20"),
    ):
        backend = FakeBackend([album_message(20), msg("-1001", 21)])
        settings = Settings(allowed_chat_ids=[] if failure == "ACCESS_DENIED" else ["-1001"])
        if failure == "AUTH_REQUIRED":
            backend.authorization_fault = ToolError(failure, "Authorization is required.")
        if failure == "CONNECTION_ERROR":
            backend.message_faults["-1001"] = ToolError(failure, "Anchor retrieval failed.")
        async with Client(
            create_server(Service(backend, settings), schema_mode="full"), mode=mode
        ) as client:
            result = await client.call_tool("get_album", {"ref": reference})
            assert result.is_error, failure
            text = "".join(block.text for block in result.content if block.type == "text")
            payload = json.loads(text[text.index("{") :])
            assert payload["code"] == failure
            assert backend.history_windows == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_album_mcp_public_channel_access_is_explicit_and_independent_of_discovery(
    mode: str,
) -> None:
    chat = Chat(id="-1001", title="Public", type="channel", username="example")
    anchor = album_message(20)
    anchor.chat = chat
    for allow_reads in (False, True):
        backend = FakeBackend([anchor], chats=[], external_chats=[chat])
        settings = Settings(
            allow_public_channel_reads=allow_reads, allow_public_channel_discovery=True
        )
        async with Client(
            create_server(Service(backend, settings), schema_mode="full"), mode=mode
        ) as client:
            result = await client.call_tool("get_album", {"ref": "https://t.me/example/20"})
            assert bool(result.is_error) is not allow_reads
            if allow_reads:
                assert result.structured_content is not None
                assert (
                    result.structured_content["messages"][0]["message_ref"].split(":")[0] == chat.id
                )
                assert backend.public_checks == [chat.id]
                assert backend.history_windows == [
                    (chat.id, 20, 30, False),
                    (chat.id, 20, 30, True),
                ]
            else:
                assert backend.reads == backend.history_windows == []
            assert "discussion_chat" not in backend.backend_calls
