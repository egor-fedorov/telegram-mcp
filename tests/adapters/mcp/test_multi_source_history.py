"""Multi-source get_history over MCP: typed budgets, errors and bounded wire size."""

import json

import pytest
from mcp.client import Client
from mcp_types import TextContent

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat, Folder
from tests.support.fakes import BASE, FakeBackend, msg

DATES = {"date_from": BASE.isoformat(), "date_to": (BASE.replace(day=8)).isoformat()}
POST = "Senior Python developer, remote. Apply via the link in the channel. " * 4


def folder_backend(count: int, per_chat: int = 5) -> FakeBackend:
    chats = [
        Chat(id=str(-1001234567000 - i), title=f"Jobs {i}", type="channel") for i in range(count)
    ]
    messages = []
    for index, chat in enumerate(chats):
        for mid in range(1, per_chat + 1):
            message = msg(chat.id, mid, POST, minute=index * per_chat + mid)
            message.chat = chat
            messages.append(message)
    folder = Folder(id=3, title="Jobs", position=0, included_chat_ids=[c.id for c in chats])
    return FakeBackend(messages, chats=chats, folders=[folder])


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_folder_history_reports_budget_and_stays_within_wire_limits(mode):
    backend = folder_backend(20)
    server = create_server(build_service(backend, Settings(allowed_folder_ids=[3])))
    async with Client(server, mode=mode) as client:
        args = {"folder_ids": [3], **DATES, "limit": 50}
        calls, total_bytes, keys = 0, 0, set()
        cursor = None
        while True:
            result = await client.call_tool(
                "get_history", {**args, **({"cursor": cursor} if cursor else {})}
            )
            assert not result.is_error and result.structured_content is not None
            payload = result.structured_content
            assert isinstance(result.content[0], TextContent)
            assert json.loads(result.content[0].text) == payload
            calls += 1
            total_bytes += len(result.content[0].text.encode())
            assert payload["ordering"] == "page"
            assert set(payload["budget"]) == {
                "max_sources",
                "max_messages",
                "emitted_messages",
                "exhausted",
            }
            assert payload["budget"]["max_sources"] == 20
            assert payload["budget"]["max_messages"] == 100
            assert len(json.dumps(payload["scope"]).encode()) < 2048
            keys.update(m["message_ref"] for m in payload["messages"])
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        # 20 sources x 5 posts at the default 100-message budget: one complete traversal.
        assert len(keys) == 100 and payload["scope"]["complete"]
        assert not payload["budget"]["exhausted"] and calls <= 4
        assert total_bytes < 100 * 1024, total_bytes


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_source_limit_error_is_structured_and_reads_nothing(mode):
    backend = folder_backend(21, per_chat=1)
    server = create_server(build_service(backend, Settings(allowed_folder_ids=[3])))
    async with Client(server, mode=mode) as client:
        result = await client.call_tool("get_history", {"folder_ids": [3], **DATES})
        assert result.is_error
        assert isinstance(result.content[0], TextContent)
        issue = json.loads(result.content[0].text.split(": ", 1)[-1])
        assert issue["code"] == "SOURCE_LIMIT_EXCEEDED"
        assert "21 sources" in issue["message"]
        assert backend.fetches == []
        bounded = await client.call_tool(
            "get_history", {"folder_ids": [3], **DATES, "max_sources": 21, "max_messages": 5}
        )
        payload = bounded.structured_content
        assert payload is not None and payload["budget"]["exhausted"]
        assert len(payload["messages"]) == 5 and payload["next_cursor"]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_single_chat_history_response_is_unchanged(mode):
    backend = FakeBackend([msg("1", mid) for mid in range(1, 4)])
    server = create_server(build_service(backend, Settings(allowed_chat_ids=["1"])))
    async with Client(server, mode=mode) as client:
        result = await client.call_tool("get_history", {"chat_id": "1"})
        payload = result.structured_content
        assert payload is not None and payload["ordering"] == "global"
        assert "budget" not in payload and "progress" not in payload
