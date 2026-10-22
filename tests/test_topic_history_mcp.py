import json
from datetime import timedelta

import pytest
from mcp.client import Client

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service as Service
from telegram_mcp.domain.models import Chat, ForumTopic, ToolError
from tests.fakes import BASE, FakeBackend, FakeClock, msg


def topic_history_backend() -> FakeBackend:
    forum = Chat(
        id="-1001",
        title="Project forum",
        username="project_forum",
        type="group",
        is_supergroup=True,
        is_forum=True,
    )
    messages = [
        msg(forum.id, mid, f"Topic {topic_id}, message {mid}").model_copy(
            update={"chat": forum, "topic_id": topic_id}
        )
        for mid, topic_id in ((10, 10), (11, 1), (12, 10), (13, 1), (20, 20))
    ]
    backend = FakeBackend(messages, chats=[forum])
    backend.pinned_message_keys = {f"{forum.id}:{mid}" for mid in (10, 11, 20)}
    backend.topics = {
        (forum.id, topic_id): ForumTopic(
            id=topic_id,
            chat_id=forum.id,
            title="General" if topic_id == 1 else f"Topic {topic_id}",
            is_general=topic_id == 1,
        )
        for topic_id in (1, 10, 20)
    }
    return backend


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_topic_history_mcp_schema_and_unchanged_chat_wide_defaults(mode: str) -> None:
    backend = topic_history_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        listing = await client.list_tools()
        assert len(listing.tools) == 14
        tools = {tool.name: tool for tool in listing.tools}
        for name in ("get_history", "get_pinned_messages"):
            tool = tools[name]
            assert tool.annotations is not None and tool.annotations.read_only_hint
            properties = tool.input_schema["properties"]
            topic_schema = properties["topic_id"]
            assert topic_schema["default"] is None
            assert "topic_id" not in tool.input_schema.get("required", [])
            topic_integer = next(
                branch for branch in topic_schema["anyOf"] if branch["type"] == "integer"
            )
            assert topic_integer["exclusiveMinimum"] == 0
            assert topic_integer["maximum"] == 2147483647
            assert tool.output_schema is not None
            assert "topic_id" in tool.output_schema["properties"]

        for name, expected in (
            ("get_history", [20, 13, 12, 11, 10]),
            ("get_pinned_messages", [20, 11, 10]),
        ):
            result = await client.call_tool(name, {"chat_ref": "@project_forum"})
            assert not result.is_error and result.structured_content is not None
            data = result.structured_content
            assert "topic_id" not in data
            assert [
                int(message["message_ref"].split(":")[1]) for message in data["messages"]
            ] == expected


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_topic_history_mcp_general_and_named_topics_dispatch(mode: str) -> None:
    backend = topic_history_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        for selector, topic_id, expected in (
            ({"chat_id": "-1001"}, 10, [10, 12]),
            ({"chat_ref": "@project_forum"}, 1, [11, 13]),
        ):
            result = await client.call_tool(
                "get_history",
                {
                    **selector,
                    "topic_id": topic_id,
                    "date_from": BASE.isoformat(),
                    "date_to": (BASE + timedelta(minutes=20)).isoformat(),
                    "order": "oldest_first",
                },
            )
            assert not result.is_error and result.structured_content is not None
            data = result.structured_content
            assert data["topic_id"] == topic_id
            assert "thread_root" not in data
            assert data["order"] == "oldest_first"
            assert data["scope"]["complete"] is True
            assert [
                int(message["message_ref"].split(":")[1]) for message in data["messages"]
            ] == expected
            assert all(message["topic_id"] == topic_id for message in data["messages"])

        for topic_id, expected in ((1, [11]), (10, [10]), (20, [20])):
            result = await client.call_tool(
                "get_pinned_messages", {"chat_ref": "@project_forum", "topic_id": topic_id}
            )
            assert not result.is_error and result.structured_content is not None
            data = result.structured_content
            assert data["topic_id"] == topic_id
            assert "thread_root" not in data
            assert [
                int(message["message_ref"].split(":")[1]) for message in data["messages"]
            ] == expected


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_topic_history_mcp_rejects_invalid_topic_ids_before_backend_calls(mode: str) -> None:
    backend = topic_history_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        for name in ("get_history", "get_pinned_messages"):
            for topic_id in (0, -1, 2147483648, True, "1", 1.5, 1.0):
                result = await client.call_tool(
                    name, {"chat_ref": "@project_forum", "topic_id": topic_id}
                )
                assert result.is_error
        assert backend.backend_calls == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_topic_history_mcp_public_flags_do_not_authorize_forums(mode: str) -> None:
    backend = topic_history_backend()
    server = create_server(
        Service(
            backend,
            Settings(allow_public_channel_reads=True, allow_public_channel_discovery=True),
        )
    )
    async with Client(server, mode=mode) as client:
        for name in ("get_history", "get_pinned_messages"):
            result = await client.call_tool(name, {"chat_ref": "@project_forum", "topic_id": 1})
            assert result.is_error
            text = "".join(block.text for block in result.content if block.type == "text")
            assert json.loads(text[text.index("{") :])["code"] == "ACCESS_DENIED"
        assert backend.fetches == []
        assert backend.public_checks == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_topic_history_mcp_exposes_unavailable_topic_and_forum_errors(mode: str) -> None:
    backend = topic_history_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        for name in ("get_history", "get_pinned_messages"):
            missing = await client.call_tool(name, {"chat_ref": "@project_forum", "topic_id": 999})
            assert missing.is_error
            text = "".join(block.text for block in missing.content if block.type == "text")
            assert json.loads(text[text.index("{") :])["code"] == "TOPIC_UNAVAILABLE"
        backend.chats[0].is_forum = False
        for name in ("get_history", "get_pinned_messages"):
            unsupported = await client.call_tool(
                name, {"chat_ref": "@project_forum", "topic_id": 1}
            )
            assert unsupported.is_error
            text = "".join(block.text for block in unsupported.content if block.type == "text")
            assert json.loads(text[text.index("{") :])["code"] == "FORUM_UNAVAILABLE"
        assert backend.fetches == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_topic_history_mcp_cursor_remains_bound_to_original_topic(mode: str) -> None:
    backend = topic_history_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        first = await client.call_tool(
            "get_history", {"chat_ref": "@project_forum", "topic_id": 1, "limit": 1}
        )
        assert not first.is_error and first.structured_content is not None
        cursor = first.structured_content["next_cursor"]
        assert cursor is not None
        assert int(first.structured_content["messages"][0]["message_ref"].split(":")[1]) == 13
        for topic_arguments in ({}, {"topic_id": 10}):
            mismatch = await client.call_tool(
                "get_history",
                {"chat_ref": "@project_forum", "cursor": cursor, **topic_arguments},
            )
            assert mismatch.is_error
            text = "".join(block.text for block in mismatch.content if block.type == "text")
            assert json.loads(text[text.index("{") :])["code"] == "CURSOR_MISMATCH"
        resumed = await client.call_tool(
            "get_history",
            {"chat_ref": "@project_forum", "topic_id": 1, "cursor": cursor, "limit": 30},
        )
        assert not resumed.is_error and resumed.structured_content is not None
        assert resumed.structured_content["topic_id"] == 1
        assert int(resumed.structured_content["messages"][0]["message_ref"].split(":")[1]) == 11
        assert resumed.structured_content["next_cursor"] is None


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_topic_history_mcp_lookup_failure_preserves_original_cursor(mode: str) -> None:
    backend = topic_history_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        arguments = {"chat_ref": "@project_forum", "topic_id": 1, "limit": 1}
        first = await client.call_tool("get_history", arguments)
        assert not first.is_error and first.structured_content is not None
        cursor = first.structured_content["next_cursor"]
        assert cursor is not None
        fetches = list(backend.fetches)
        backend.topic_lookup_faults[("-1001", 1)] = ToolError(
            "CONNECTION_ERROR", "Could not verify the topic."
        )
        failed = await client.call_tool("get_history", {**arguments, "cursor": cursor})
        assert failed.is_error
        text = "".join(block.text for block in failed.content if block.type == "text")
        assert json.loads(text[text.index("{") :])["code"] == "CONNECTION_ERROR"
        assert backend.fetches == fetches
        backend.topic_lookup_faults.clear()
        resumed = await client.call_tool("get_history", {**arguments, "cursor": cursor})
        assert not resumed.is_error and resumed.structured_content is not None
        assert int(resumed.structured_content["messages"][0]["message_ref"].split(":")[1]) == 11
        assert resumed.structured_content["next_cursor"] is None


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("name", ["get_history", "get_pinned_messages"])
async def test_topic_history_mcp_temporary_fetch_failure_retains_topic_and_backoff(
    mode: str, name: str
) -> None:
    backend = topic_history_backend()
    backend.faults["-1001"] = ToolError("RATE_LIMITED", "Wait before retrying.", 37)
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"]), clock=FakeClock())
    )
    async with Client(server, mode=mode) as client:
        arguments = {"chat_ref": "@project_forum", "topic_id": 1}
        result = await client.call_tool(name, arguments)
        assert not result.is_error and result.structured_content is not None
        data = result.structured_content
        assert data["topic_id"] == 1
        assert data["scope"]["complete"] is False
        assert data["next_cursor"]
        assert data["issues"][0]["code"] == "RATE_LIMITED"
        assert data["issues"][0]["retry_after_seconds"] == 37
        backend.faults.clear()
        retry = await client.call_tool(name, {**arguments, "cursor": data["next_cursor"]})
        assert not retry.is_error and retry.structured_content is not None
        assert retry.structured_content["topic_id"] == 1
        assert retry.structured_content["next_cursor"]
        assert len(backend.fetches) == 1
