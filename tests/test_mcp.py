import json
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.application.ports import DiscoveryBatch
from telegram_mcp.bootstrap import build_service as Service
from telegram_mcp.domain.models import Folder, PublicChannel, ToolError
from tests.fakes import FakeBackend, msg

EXPECTED_TOOLS = {
    "get_status",
    "search_public_channels",
    "get_similar_channels",
    "list_folders",
    "list_chats",
    "get_chat",
    "search_messages",
    "get_history",
    "list_topics",
    "get_pinned_messages",
    "get_messages",
    "get_album",
    "get_context",
    "get_thread",
}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_tool_schemas_and_real_dispatch(mode: str) -> None:
    backend = FakeBackend(
        [msg("1", 1)], folders=[Folder(id=1, title="Work", position=0, included_chat_ids=["1"])]
    )
    server = create_server(
        Service(backend, Settings(allowed_folder_ids=[1], allow_public_channel_reads=True))
    )
    async with Client(server, mode=mode) as client:
        listing = await client.list_tools()
        assert {t.name for t in listing.tools} == EXPECTED_TOOLS
        assert all(t.annotations and t.annotations.read_only_hint for t in listing.tools)
        history_schema = next(t.input_schema for t in listing.tools if t.name == "get_history")
        assert {"chat_id", "chat_ref"} <= history_schema["properties"].keys()
        assert "chat_id" not in history_schema.get("required", [])
        status = await client.call_tool("get_status", {})
        assert status.structured_content is not None
        assert status.structured_content["allow_public_channel_reads"] is True
        assert status.structured_content["allow_public_channel_discovery"] is False
        result = await client.call_tool("search_messages", {"query": "budget", "folder_ids": [1]})
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["messages"][0]["message_ref"]["message_id"] == 1
        invalid = await client.call_tool("get_history", {"chat_id": "1", "limit": 101})
        assert invalid.is_error
        bad_date = await client.call_tool(
            "get_history", {"chat_id": "1", "date_from": "2026-09-01T00:00:00"}
        )
        assert bad_date.is_error
        for selector in ({"chat_id": "1"}, {"chat_ref": "1"}):
            history = await client.call_tool("get_history", selector)
            assert not history.is_error
            assert history.structured_content is not None
            assert history.structured_content["messages"][0]["message_ref"]["chat_id"] == "1"
        for selector in ({}, {"chat_id": "1", "chat_ref": "1"}):
            assert (await client.call_tool("get_history", selector)).is_error
        folder_result = await client.call_tool("list_folders", {})
        assert not folder_result.is_error
        assert (await client.call_tool("get_context", {"ref": "1:1"})).is_error is not True
    assert backend.closed


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_discovery_schemas_dispatch_and_failures(mode: str) -> None:
    backend = FakeBackend()
    channels = [
        PublicChannel(
            id=f"-100{n}", title=f"Source {n}", username=f"source{n}", url=f"https://t.me/source{n}"
        )
        for n in (1, 2)
    ]
    backend.discovery_search_batch = DiscoveryBatch([channels[0], channels[0], channels[1]])
    backend.discovery_similar_batch = DiscoveryBatch(channels, telegram_total_count=50)
    server = create_server(Service(backend, Settings(allow_public_channel_discovery=True)))
    async with Client(server, mode=mode) as client:
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        for name, parameter in (
            ("search_public_channels", "query"),
            ("get_similar_channels", "chat_ref"),
        ):
            tool = tools[name]
            assert tool.annotations is not None and tool.annotations.read_only_hint
            assert tool.input_schema["required"] == [parameter]
            limit_schema = tool.input_schema["properties"]["limit"]
            assert (limit_schema["minimum"], limit_schema["maximum"], limit_schema["default"]) == (
                1,
                100,
                20,
            )
            assert "cursor" not in tool.input_schema["properties"]
            assert tool.output_schema is not None
            assert "channels" in tool.output_schema["properties"]
            assert "next_cursor" not in tool.output_schema["properties"]
        query_schema = tools["search_public_channels"].input_schema["properties"]["query"]
        assert (query_schema["minLength"], query_schema["maxLength"]) == (1, 1000)
        result = await client.call_tool("search_public_channels", {"query": "  science  "})
        assert not result.is_error
        assert backend.discovery_search_calls == [("science", 20)]
        data = result.structured_content
        assert data is not None
        assert [channel["id"] for channel in data["channels"]] == ["-1001", "-1002"]
        assert data["channels"][0]["participants_count"] is None
        assert data["returned_count"] == 2
        assert data["telegram_total_count"] is None
        assert data["exhaustive"] is False
        assert data["locally_truncated"] is False
        similar = await client.call_tool(
            "get_similar_channels", {"chat_ref": " https://t.me/s/source1 ", "limit": 1}
        )
        assert not similar.is_error
        assert backend.discovery_similar_calls == ["https://t.me/s/source1"]
        assert similar.structured_content is not None
        assert similar.structured_content["returned_count"] == 1
        assert similar.structured_content["telegram_total_count"] == 50
        assert similar.structured_content["locally_truncated"] is True
        calls = list(backend.backend_calls)
        for name, arguments in (
            ("search_public_channels", {}),
            ("search_public_channels", {"query": " "}),
            ("search_public_channels", {"query": "q" * 1001}),
            ("search_public_channels", {"query": "science", "limit": 0}),
            ("get_similar_channels", {}),
            ("get_similar_channels", {"chat_ref": " "}),
            ("get_similar_channels", {"chat_ref": "@source1", "limit": 101}),
        ):
            assert (await client.call_tool(name, arguments)).is_error
        assert backend.backend_calls == calls
        for name, arguments, key in (
            ("search_public_channels", {"query": "science"}, "search"),
            ("get_similar_channels", {"chat_ref": "@source1"}, "similar"),
        ):
            for error in (
                ToolError("RATE_LIMITED", "Telegram requests a wait of 37 seconds.", 37),
                ToolError("CONNECTION_ERROR", "Connection failed."),
                ToolError("TELEGRAM_UNAVAILABLE", "Telegram is temporarily unavailable."),
            ):
                backend.discovery_faults[key] = error
                failure = await client.call_tool(name, arguments)
                assert failure.is_error
                text = "".join(block.text for block in failure.content if block.type == "text")
                assert error.code in text and error.detail in text
                payload = json.loads(text[text.index("{") :])
                assert payload["code"] == error.code
                if error.retry_after is not None:
                    assert payload["retry_after_seconds"] == error.retry_after


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_disabled_discovery_never_contacts_backend(mode: str) -> None:
    backend = FakeBackend()
    server = create_server(
        Service(backend, Settings(allow_all_chats=True, allow_public_channel_reads=True))
    )
    async with Client(server, mode=mode) as client:
        for name, arguments in (
            ("search_public_channels", {"query": "science"}),
            ("get_similar_channels", {"chat_ref": "@source1"}),
        ):
            result = await client.call_tool(name, arguments)
            assert result.is_error
            text = "".join(block.text for block in result.content if block.type == "text")
            assert "FEATURE_DISABLED" in text
        assert backend.backend_calls == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_exposes_expected_retrieval_errors_and_redacts_unexpected_failures(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend([msg("1", 1)])
    server = create_server(Service(backend, Settings()))
    async with Client(server, mode=mode) as client:
        denied = await client.call_tool("get_history", {"chat_id": "1"})
        assert denied.is_error
        text = "".join(block.text for block in denied.content if block.type == "text")
        assert json.loads(text[text.index("{") :])["code"] == "ACCESS_DENIED"

        async def broken_catalog() -> object:
            raise RuntimeError("sensitive internal detail")

        monkeypatch.setattr(backend, "catalog", broken_catalog)
        failure = await client.call_tool("list_chats", {})
        assert failure.is_error
        text = "".join(block.text for block in failure.content if block.type == "text")
        assert "Error executing tool list_chats" in text
        assert "sensitive internal detail" not in text


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_process_starts_without_credentials(tmp_path: Path, mode: str) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "telegram_mcp.cli", "--config", str(tmp_path / "missing.toml"), "serve"],
        env={"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": "", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with Client(parameters, mode=mode, read_timeout_seconds=10) as client:
        listing = await client.list_tools()
        assert {t.name for t in listing.tools} == EXPECTED_TOOLS
        response = await client.call_tool("get_status", {})
        assert not response.is_error
        assert response.structured_content is not None
        assert response.structured_content["authorized"] is False
        assert response.structured_content["allow_public_channel_reads"] is False
        assert response.structured_content["allow_public_channel_discovery"] is False
        assert response.structured_content["error"]["code"] == "AUTH_REQUIRED"
