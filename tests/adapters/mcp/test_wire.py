"""Public compact response semantics and reproducible wire-size regression checks."""

import json

import pytest
from mcp.client import Client
from mcp_types import CallToolResult

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import (
    Entity,
    ForwardOrigin,
    MessageRef,
    PollAnswerResult,
    SearchProgress,
    ToolError,
    WebPage,
)
from tests.support.fakes import BASE, FakeBackend, msg
from tests.support.message_metadata import rich_message
from tests.support.navigation import navigation_backend

# Frozen 0.2.0 MCP 2.2 measurements, before adding the compact projection. Both
# include the complete MCP result: text fallback AND structured content. The
# fixture below contains 30 ordinary posts, 342 characters each (10,260 total).
BASELINE_RESULT_BYTES = 83_588
BASELINE_TOOLS_BYTES = 110_590


def assert_mirrored(result: CallToolResult) -> dict:
    assert not result.is_error
    assert result.structured_content is not None
    assert len(result.content) == 1 and result.content[0].type == "text"
    text = result.content[0].text
    assert text == json.dumps(result.structured_content, ensure_ascii=False, separators=(",", ":"))
    assert json.loads(text) == result.structured_content
    return result.structured_content


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_compact_full_wire_size_budgets_keep_every_text_character(mode: str) -> None:
    text = "Project update: the weekly delivery remains on schedule. " * 6
    assert len(text) * 30 == 10_260
    backend = FakeBackend([msg("1", mid, text) for mid in range(1, 31)])
    async with Client(
        create_server(build_service(backend, Settings(allowed_chat_ids=["1"]))), mode=mode
    ) as client:
        listing = await client.list_tools()
        result = await client.call_tool("get_history", {"chat_id": "1", "limit": 30})
        payload = assert_mirrored(result)
        assert len(payload["messages"]) == 30
        assert [message["message_ref"] for message in payload["messages"]] == [
            f"1:{mid}" for mid in range(30, 0, -1)
        ]
        assert all(message["text"] == text for message in payload["messages"])
        assert all(message["next_text_offset"] is None for message in payload["messages"])
        assert list(payload["chats"]) == ["1"]
        assert payload["chats"]["1"] == {"title": "Chat 1", "type": "group"}
        assert all("chat" not in message for message in payload["messages"])
        result_bytes = len(result.model_dump_json(by_alias=True, exclude_unset=True).encode())
        # Preserve the original retrieval budget independently of additive M4 tools.
        original_tools = listing.model_copy(
            update={
                "tools": [
                    tool
                    for tool in listing.tools
                    if tool.name not in {"get_changes", "export_history"}
                ]
            }
        )
        tools_bytes = len(
            original_tools.model_dump_json(by_alias=True, exclude_unset=True).encode()
        )
        assert result_bytes <= BASELINE_RESULT_BYTES * 0.40, result_bytes
        assert tools_bytes <= BASELINE_TOOLS_BYTES * 0.75, tools_bytes


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_all_fourteen_tools_have_typed_self_contained_mirrored_responses(mode: str) -> None:
    backend = navigation_backend()
    backend.stored_messages[0].album_id = "42"
    server = create_server(
        build_service(
            backend,
            Settings(allowed_chat_ids=["-1001"], allow_public_channel_discovery=True),
        ),
        schema_mode="full",
    )
    calls = {
        "get_status": {},
        "search_public_channels": {"query": "project"},
        "get_similar_channels": {"chat_ref": "@project"},
        "list_folders": {},
        "list_chats": {},
        "get_chat": {"chat_ref": "-1001"},
        "list_topics": {"chat_ref": "-1001"},
        "get_pinned_messages": {"chat_ref": "-1001"},
        "search_messages": {"query": "Topic", "chat_ids": ["-1001"]},
        "get_history": {"chat_id": "-1001"},
        "get_messages": {"refs": ["-1001:10"]},
        "get_album": {"ref": "-1001:10"},
        "get_context": {"ref": "-1001:10"},
        "get_thread": {"ref": "-1001:10"},
    }
    async with Client(server, mode=mode) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == len(calls) + 9 == 23
        for tool in tools:
            schema = tool.output_schema
            assert schema is not None
            assert schema["type"] == "object" and schema["additionalProperties"] is False
            assert schema["properties"] and schema["required"]
            definitions = schema.get("$defs", {})

            def check_refs(value: object, definitions: dict = definitions) -> None:
                if isinstance(value, dict):
                    if "$ref" in value:
                        ref = value["$ref"]
                        assert ref.startswith("#/$defs/")
                        assert ref.removeprefix("#/$defs/") in definitions
                    for child in value.values():
                        check_refs(child)
                elif isinstance(value, list):
                    for child in value:
                        check_refs(child)

            check_refs(schema)
            if tool.name not in calls:
                continue  # Success/resource parity is covered in test_attachments_mcp.
            payload = assert_mirrored(await client.call_tool(tool.name, calls[tool.name]))
            if tool.name == "list_folders":
                assert payload == {"folders": []}
            elif tool.name == "get_chat":
                assert payload["chat"]["title"] == "Project forum"
                assert payload["server_can_send"] is False
                assert "linked_chat_id" not in payload


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_sparse_metadata_keeps_zero_false_references_and_web_preview(mode: str) -> None:
    message = rich_message(20)
    message.has_link = True
    message.web_page = WebPage(
        status="available",
        id="9007199254740999",
        url="https://example.org/😀",
        title="Source title",
        description="Résumé\nSecond line",
    )
    message.entities.append(Entity(type="MessageEntityTextUrl", offset=0, length=1, url=""))
    message.reply_to = MessageRef(chat_id="-1001", message_id=19)
    message.thread_ref = MessageRef(chat_id="-1001", message_id=18)
    message.forward_origin = ForwardOrigin(
        message_ref=MessageRef(chat_id="-1009", message_id=3),
        saved_from_ref=MessageRef(chat_id="-1008", message_id=2),
        post_author="Author",
        saved_from_name="Hidden",
        saved_at=BASE,
    )
    assert message.poll is not None and message.poll.results is not None
    message.poll.results.answers = [
        PollAnswerResult(option_id="00ff", voter_count=0, is_chosen=False, is_correct=False)
    ]
    original = message.model_dump()
    backend = FakeBackend([message])
    async with Client(
        create_server(build_service(backend, Settings(allowed_chat_ids=["-1001"]))), mode=mode
    ) as client:
        result = await client.call_tool("get_messages", {"refs": ["-1001:20", "bad", "-1001:99"]})
        data = assert_mirrored(result)
        item = data["messages"][0]
        assert item["message_ref"] == "-1001:20"
        assert item["reply_to"] == "-1001:19" and item["thread_ref"] == "-1001:18"
        assert item["forward_origin"] == {
            "message_ref": "-1009:3",
            "saved_from_ref": "-1008:2",
            "post_author": "Author",
            "saved_from_name": "Hidden",
            "saved_at": "2026-09-01T00:00:00Z",
        }
        assert item["web_page"] == {
            "status": "available",
            "id": "9007199254740999",
            "url": "https://example.org/😀",
            "title": "Source title",
            "description": "Résumé\nSecond line",
        }
        assert item["entities"][-1]["url"] == ""
        assert item["has_link"] is True and item["forward_count"] == 0
        assert "reply_count" not in item and "sender" not in item
        assert item["poll"]["results"]["answers"] == [
            {"option_id": "00ff", "voter_count": 0, "is_chosen": False, "is_correct": False}
        ]
        assert data["issues"][0]["input_index"] == 1
        assert "message_ref" not in data["issues"][0]
        assert data["issues"][1]["message_ref"] == "-1001:99"
        assert data["issues"][1]["input_index"] == 2
        assert set(data["chats"]) == {"-1001"}
        assert message.model_dump() == original


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_status_unknown_authorization_and_required_controls_remain_explicit(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()

    async def unknown_status() -> dict[str, object]:
        return {
            "authorized": None,
            "connected": False,
            "error": ToolError("CONNECTION_ERROR", "Connection unavailable.").issue().model_dump(),
        }

    monkeypatch.setattr(backend, "status", unknown_status)
    async with Client(create_server(build_service(backend, Settings())), mode=mode) as client:
        data = assert_mirrored(await client.call_tool("get_status", {}))
        assert data["authorized"] is None and data["connected"] is False
        assert data["allowed_chat_ids"] == data["allowed_folder_ids"] == []
        assert data["allow_all_chats"] is data["allow_public_channel_reads"] is False
        assert data["error"] == {"code": "CONNECTION_ERROR", "message": "Connection unavailable."}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_link_search_filter_requires_query_but_accepts_empty_with_links(mode: str) -> None:
    plain, linked = msg("1", 1, "plain"), msg("1", 2, "https://example.org")
    linked.has_link = True
    backend = FakeBackend([plain, linked])
    async with Client(
        create_server(build_service(backend, Settings(allowed_chat_ids=["1"]))), mode=mode
    ) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        schema = tools["search_messages"].input_schema
        assert "query" in schema["required"]
        assert "minLength" not in schema["properties"]["query"]
        for arguments in ({"has_link": True}, {"query": ""}, {"query": "", "has_link": False}):
            assert (await client.call_tool("search_messages", arguments)).is_error
        assert backend.fetches == []
        links = assert_mirrored(
            await client.call_tool(
                "search_messages", {"query": "", "has_link": True, "chat_ids": ["1"]}
            )
        )
        assert [message["message_ref"] for message in links["messages"]] == ["1:2"]
        assert links["messages"][0]["has_link"] is True
        assert SearchProgress.model_validate(links["progress"]).sources_total == 1
