import json

import pytest
from mcp.client import Client

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.application.ports import TopicBatch, TopicOffset
from telegram_mcp.bootstrap import build_service as Service
from telegram_mcp.domain.models import Chat, ForumTopic, ToolError
from tests.fakes import BASE, FakeBackend, FakeClock, msg


def navigation_backend() -> FakeBackend:
    forum = Chat(
        id="-1001",
        title="Project forum",
        username="project_forum",
        type="group",
        is_supergroup=True,
        is_forum=True,
    )
    root = msg(forum.id, 10, "Topic root")
    reply = msg(forum.id, 12, "Topic reply", reply_to=root.message_ref)
    pin = msg(forum.id, 20, "Chat-wide pinned message")
    backend = FakeBackend([root, reply, pin], chats=[forum])
    backend.pinned_message_keys = {root.message_ref.key, pin.message_ref.key}
    backend.topic_batches[(forum.id, TopicOffset())] = TopicBatch(
        topics=[
            ForumTopic(
                id=10,
                chat_id=forum.id,
                title="Roadmap",
                created_at=BASE,
                is_closed=False,
                is_hidden=False,
                is_pinned=True,
                unread_count=2,
                root_message_ref=root.message_ref,
                top_message_ref=reply.message_ref,
                url="https://t.me/project_forum/10",
            ),
            ForumTopic(
                id=1,
                chat_id=forum.id,
                title="General",
                is_general=True,
                top_message_ref=pin.message_ref,
            ),
            ForumTopic(id=30, chat_id=forum.id, is_short=True),
            ForumTopic(id=40, chat_id=forum.id, is_deleted=True),
        ],
        next_offset=None,
        exhausted=True,
        order_by_create_date=False,
        telegram_total_count=4,
    )
    return backend


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_navigation_mcp_schemas_dispatch_and_message_references(mode: str) -> None:
    backend = navigation_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        for name, result_field in (("list_topics", "topics"), ("get_pinned_messages", "messages")):
            tool = tools[name]
            assert tool.annotations is not None and tool.annotations.read_only_hint
            assert tool.input_schema["required"] == ["chat_ref"]
            properties = tool.input_schema["properties"]
            assert properties["chat_ref"]["minLength"] == 1
            assert "cursor" in properties
            limit = properties["limit"]
            assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 100, 30)
            assert tool.output_schema is not None
            assert {result_field, "scope", "issues", "next_cursor"} <= set(
                tool.output_schema["properties"]
            )
        query_schema = tools["list_topics"].input_schema["properties"]["query"]
        query_string = next(item for item in query_schema["anyOf"] if item["type"] == "string")
        assert (query_string["minLength"], query_string["maxLength"]) == (1, 1000)
        assert "query" not in tools["get_pinned_messages"].input_schema["properties"]

        topics = await client.call_tool(
            "list_topics", {"chat_ref": " @project_forum ", "query": " Roadmap "}
        )
        assert not topics.is_error
        data = topics.structured_content
        assert data is not None
        assert [topic["id"] for topic in data["topics"]] == [10, 1, 30, 40]
        assert data["order"] == "last_message_date"
        assert data["telegram_total_count"] == 4
        assert data["next_cursor"] is None and data["scope"]["complete"] is True
        assert backend.topic_fetches == [("-1001", TopicOffset(), 30, "Roadmap")]
        ordinary, general, short, deleted = data["topics"]
        assert general["is_general"] is True and "root_message_ref" not in general
        assert "url" not in general
        assert short["is_short"] is True and deleted["is_deleted"] is True
        for topic in (short, deleted):
            for field in ("title", "created_at", "unread_count", "is_closed", "is_pinned"):
                assert field not in topic
        top = await client.call_tool("get_messages", {"refs": [ordinary["top_message_ref"]]})
        assert not top.is_error and top.structured_content is not None
        assert int(top.structured_content["messages"][0]["message_ref"].split(":")[1]) == 12
        thread = await client.call_tool("get_thread", {"ref": ordinary["root_message_ref"]})
        assert not thread.is_error and thread.structured_content is not None
        assert int(thread.structured_content["messages"][0]["message_ref"].split(":")[1]) == 12

        pins = await client.call_tool("get_pinned_messages", {"chat_ref": " -1001 "})
        assert not pins.is_error and pins.structured_content is not None
        assert pins.structured_content["order"] == "newest_first"
        assert [
            int(message["message_ref"].split(":")[1])
            for message in pins.structured_content["messages"]
        ] == [20, 10]
        assert backend.fetch_pinned_flags[-1] is True


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_navigation_mcp_rejects_invalid_arguments_before_backend_calls(mode: str) -> None:
    backend = navigation_backend()
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        for name in ("list_topics", "get_pinned_messages"):
            for arguments in (
                {},
                {"chat_ref": " "},
                {"chat_ref": "-1001", "limit": 0},
                {"chat_ref": "-1001", "limit": 101},
            ):
                assert (await client.call_tool(name, arguments)).is_error
        for query in (" ", "x" * 1001):
            assert (
                await client.call_tool("list_topics", {"chat_ref": "-1001", "query": query})
            ).is_error
        assert backend.backend_calls == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_navigation_mcp_public_flags_do_not_grant_forum_access(mode: str) -> None:
    backend = navigation_backend()
    server = create_server(
        Service(
            backend,
            Settings(allow_public_channel_reads=True, allow_public_channel_discovery=True),
        )
    )
    async with Client(server, mode=mode) as client:
        for name in ("list_topics", "get_pinned_messages"):
            result = await client.call_tool(name, {"chat_ref": "@project_forum"})
            assert result.is_error
            text = "".join(block.text for block in result.content if block.type == "text")
            assert json.loads(text[text.index("{") :])["code"] == "ACCESS_DENIED"
        assert backend.topic_fetches == []
        assert backend.fetches == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_navigation_mcp_reports_unsupported_forum_after_access_check(mode: str) -> None:
    backend = navigation_backend()
    backend.chats[0].is_forum = False
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        result = await client.call_tool("list_topics", {"chat_ref": "-1001"})
        assert result.is_error
        text = "".join(block.text for block in result.content if block.type == "text")
        assert json.loads(text[text.index("{") :])["code"] == "FORUM_UNAVAILABLE"
        assert backend.topic_fetches == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("name", ["list_topics", "get_pinned_messages"])
async def test_navigation_mcp_transient_fetch_failures_keep_cursor_and_delay(
    mode: str, name: str
) -> None:
    backend = navigation_backend()
    fault = ToolError("RATE_LIMITED", "Wait before retrying.", 37)
    faults = backend.topic_faults if name == "list_topics" else backend.faults
    faults["-1001"] = fault
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"]), clock=FakeClock())
    )
    async with Client(server, mode=mode) as client:
        result = await client.call_tool(name, {"chat_ref": "-1001"})
        assert not result.is_error
        data = result.structured_content
        assert data is not None
        assert data["scope"]["complete"] is False
        assert data["next_cursor"]
        assert data["issues"][0]["code"] == "RATE_LIMITED"
        assert data["issues"][0]["retry_after_seconds"] == 37
        faults.clear()
        retry = await client.call_tool(name, {"chat_ref": "-1001", "cursor": data["next_cursor"]})
        assert not retry.is_error and retry.structured_content is not None
        assert retry.structured_content["next_cursor"]
        assert len(backend.topic_fetches if name == "list_topics" else backend.fetches) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_navigation_mcp_short_topic_page_can_continue(mode: str) -> None:
    backend = navigation_backend()
    offset = TopicOffset(BASE, 12, 10)
    topic = backend.topic_batches[("-1001", TopicOffset())].topics[0]
    backend.topic_batches[("-1001", TopicOffset())] = TopicBatch(
        topics=[topic],
        next_offset=offset,
        exhausted=False,
        order_by_create_date=True,
        telegram_total_count=2,
    )
    backend.topic_batches[("-1001", offset)] = TopicBatch(
        topics=[ForumTopic(id=20, chat_id="-1001", title="Second topic")],
        next_offset=None,
        exhausted=True,
        order_by_create_date=True,
        telegram_total_count=2,
    )
    server = create_server(Service(backend, Settings(allowed_chat_ids=["-1001"])))
    async with Client(server, mode=mode) as client:
        first = await client.call_tool("list_topics", {"chat_ref": "-1001", "limit": 30})
        assert not first.is_error and first.structured_content is not None
        data = first.structured_content
        assert [topic["id"] for topic in data["topics"]] == [10]
        assert data["scope"]["complete"] is False
        assert data["order"] == "creation_date"
        assert data["next_cursor"] and len(backend.topic_fetches) == 1
        second = await client.call_tool(
            "list_topics", {"chat_ref": "-1001", "limit": 30, "cursor": data["next_cursor"]}
        )
        assert not second.is_error and second.structured_content is not None
        assert [topic["id"] for topic in second.structured_content["topics"]] == [20]
        assert second.structured_content["scope"]["complete"] is True
        assert second.structured_content["next_cursor"] is None
        assert len(backend.topic_fetches) == 2
