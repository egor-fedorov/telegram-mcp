import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from telegram_mcp.config import Settings
from telegram_mcp.models import Folder
from telegram_mcp.server import create_server
from telegram_mcp.service import Service
from tests.fakes import FakeBackend, msg

EXPECTED_TOOLS = {
    "get_status",
    "list_folders",
    "list_chats",
    "get_chat",
    "search_messages",
    "get_history",
    "get_messages",
    "get_context",
    "get_thread",
}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_tool_schemas_and_real_dispatch(mode: str) -> None:
    backend = FakeBackend(
        [msg("1", 1)], folders=[Folder(id=1, title="Work", position=0, included_chat_ids=["1"])]
    )
    server = create_server(Service(backend, Settings(allowed_folder_ids=[1])))
    async with Client(server, mode=mode) as client:
        listing = await client.list_tools()
        assert {t.name for t in listing.tools} == EXPECTED_TOOLS
        assert all(t.annotations and t.annotations.read_only_hint for t in listing.tools)
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
        folder_result = await client.call_tool("list_folders", {})
        assert not folder_result.is_error
        assert (await client.call_tool("get_context", {"ref": "1:1"})).is_error is not True
    assert backend.closed


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
        assert response.structured_content["error"]["code"] == "AUTH_REQUIRED"
