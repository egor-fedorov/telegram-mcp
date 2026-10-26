import json

import pytest
from mcp.client import Client

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service as Service
from telegram_mcp.domain.models import (
    Chat,
    ForumTopic,
    Message,
    PollResults,
    ReactionSummary,
)
from tests.support.fakes import FakeBackend, msg
from tests.support.message_metadata import METADATA_FIELDS, assert_metadata, rich_message


def rich_backend(messages: list[Message]) -> FakeBackend:
    forum = Chat(
        id="-1001", title="Metadata forum", type="group", is_supergroup=True, is_forum=True
    )
    backend = FakeBackend(messages, chats=[forum])
    backend.topics[(forum.id, 7)] = ForumTopic(id=7, chat_id=forum.id, title="Metadata")
    backend.pinned_message_keys = {message.message_ref.key for message in messages}
    return backend


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_metadata_mcp_schemas_preserve_read_only_surface_without_actor_lists(
    mode: str,
) -> None:
    server = create_server(Service(FakeBackend(), Settings()), schema_mode="full")
    async with Client(server, mode=mode) as client:
        listing = await client.list_tools()
        assert len(listing.tools) == 22
        for tool in listing.tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint == (
                tool.name
                not in {
                    "download_attachment",
                    "get_changes",
                    "export_history",
                    "create_watch",
                    "get_watch_changes",
                    "ack_watch_changes",
                    "delete_watch",
                }
            )
            if tool.name not in {
                "search_messages",
                "get_history",
                "get_pinned_messages",
                "get_messages",
                "get_album",
                "get_context",
                "get_thread",
            }:
                continue
            assert tool.output_schema is not None
            definitions = tool.output_schema["$defs"]
            properties = definitions["Message"]["properties"]
            assert set(METADATA_FIELDS) <= properties.keys()
            for field in ("view_count", "forward_count", "reply_count", "reactions", "poll"):
                assert "anyOf" not in properties[field]
                assert field not in definitions["Message"]["required"]
            assert "option_id" in definitions["PollOption"]["properties"]
            assert "option_id" in definitions["PollAnswerResult"]["properties"]
            assert "reactions_as_tags" in definitions["ReactionSummary"]["properties"]
            for omitted in (
                "recent_reactions",
                "recent_voters",
                "top_reactors",
                "recent_repliers",
                "access_hash",
            ):
                assert omitted not in json.dumps(tool.output_schema)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_metadata_mcp_get_messages_keeps_duplicates_and_unicode_continuations(
    mode: str,
) -> None:
    message = rich_message()
    original = message.model_dump(mode="json")
    backend = rich_backend([message])
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        refs = [message.message_ref.key, message.message_ref.key]
        first = await client.call_tool("get_messages", {"refs": refs, "text_limit": 2})
        assert not first.is_error and first.structured_content is not None
        assert len(first.structured_content["messages"]) == 2
        for item in first.structured_content["messages"]:
            assert item["text"] == "A😀"
            assert item["next_text_offset"] == 2
            assert item["text_total_length"] == len(message.text)
            assert item["content_type"] == "other"
            assert_metadata(item, message)
        second = await client.call_tool(
            "get_messages", {"refs": refs, "text_offset": 2, "text_limit": 2}
        )
        assert not second.is_error and second.structured_content is not None
        for item in second.structured_content["messages"]:
            assert item["text"] == "B " and item["text_offset"] == 2
            assert_metadata(item, message)
        assert backend.batch_reads == [("-1001", [20]), ("-1001", [20])]
        assert backend.fetches == [] and backend.reads == []
        assert message.model_dump(mode="json") == original


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_history", {"chat_id": "-1001"}),
        ("get_history", {"chat_ref": "-1001", "topic_id": 7}),
        ("get_pinned_messages", {"chat_ref": "-1001"}),
        ("get_pinned_messages", {"chat_ref": "-1001", "topic_id": 7}),
        ("search_messages", {"query": "A", "chat_ids": ["-1001"], "ordering": "global"}),
    ],
)
async def test_metadata_mcp_pagination_and_text_budget_preserve_nested_fields(
    mode: str, tool: str, arguments: dict[str, object]
) -> None:
    messages = [rich_message(mid, "A😀B" + "x" * 12000) for mid in (20, 21)]
    backend = rich_backend(messages)
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        first = await client.call_tool(tool, {**arguments, "limit": 1})
        assert not first.is_error and first.structured_content is not None
        page = first.structured_content
        assert len(page["messages"]) == 1 and page["next_cursor"]
        item = page["messages"][0]
        assert int(item["message_ref"].split(":")[1]) == 21
        assert len(item["text"]) == 12000 and item["next_text_offset"] == 12000
        assert_metadata(item, messages[1])
        second = await client.call_tool(
            tool, {**arguments, "limit": 1, "cursor": page["next_cursor"]}
        )
        assert not second.is_error and second.structured_content is not None
        page = second.structured_content
        assert page["next_cursor"] is None and page["scope"]["complete"] is True
        assert_metadata(page["messages"][0], messages[0])
        assert backend.fetches == ["-1001"]
        assert backend.batch_reads == [] and backend.reads == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_metadata_mcp_context_preserves_neighbors_and_parent_fields(mode: str) -> None:
    messages = [rich_message(mid) for mid in (19, 20, 21)]
    messages[1].reply_to = messages[0].message_ref
    backend = rich_backend(messages)
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        result = await client.call_tool(
            "get_context", {"ref": "-1001:20", "before": 1, "after": 1, "reply_depth": 1}
        )
        assert not result.is_error and result.structured_content is not None
        data = result.structured_content
        assert_metadata(data["anchor"], messages[1])
        assert_metadata(data["before"][0], messages[0])
        assert_metadata(data["after"][0], messages[2])
        assert_metadata(data["reply_chain"][0], messages[0])
        assert backend.reads == ["-1001:20", "-1001:19"]
        assert backend.fetches == ["-1001", "-1001"] and backend.batch_reads == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_metadata_mcp_preserves_unknown_zero_and_empty_result_distinctions(mode: str) -> None:
    unknown = msg("-1001", 1)
    zero = rich_message(2)
    zero.view_count = zero.forward_count = zero.reply_count = 0
    zero.reactions = ReactionSummary(results=[])
    zero.reactions.reactions_as_tags = True
    assert zero.poll is not None
    zero.poll.results = PollResults(total_voters=0, answers=[])
    partial = rich_message(3)
    assert partial.poll is not None
    partial.poll.results = PollResults(is_min=True)
    backend = rich_backend([unknown, zero, partial])
    server = create_server(
        Service(backend, Settings(allowed_chat_ids=["-1001"])), schema_mode="full"
    )
    async with Client(server, mode=mode) as client:
        result = await client.call_tool("get_messages", {"refs": ["-1001:1", "-1001:2", "-1001:3"]})
        assert not result.is_error and result.structured_content is not None
        absent, empty, incomplete = result.structured_content["messages"]
        assert all(field not in absent for field in METADATA_FIELDS)
        assert empty["view_count"] == empty["forward_count"] == empty["reply_count"] == 0
        assert empty["reactions"]["results"] == []
        assert empty["reactions"]["reactions_as_tags"] is True
        assert empty["poll"]["results"]["total_voters"] == 0
        assert empty["poll"]["results"]["answers"] == []
        assert "total_voters" not in incomplete["poll"]["results"]
        assert "answers" not in incomplete["poll"]["results"]
        assert backend.batch_reads == [("-1001", [1, 2, 3])]
