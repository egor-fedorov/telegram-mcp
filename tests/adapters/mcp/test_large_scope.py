"""Full MCP payload budgets for large catalog and search selections."""

import json

import pytest
from mcp.client import Client
from mcp_types import TextContent

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat
from tests.support.fakes import FakeBackend, msg


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("count", [20, 21, 300, 3000])
async def test_scope_is_bounded_without_changing_source_selection(mode, count):
    chats = [
        Chat(id=str(-1001234567000 - i), title=f"Channel {i}", type="channel") for i in range(count)
    ]
    backend = FakeBackend([msg(c.id, 1) for c in chats], chats=chats)
    async with Client(
        create_server(build_service(backend, Settings(allow_all_chats=True))), mode=mode
    ) as client:
        for tool, args, budget in (
            ("list_chats", {"limit": 30}, 32 * 1024),
            ("search_messages", {"query": "budget", "limit": 10}, 24 * 1024),
        ):
            first = await client.call_tool(tool, args)
            assert not first.is_error and first.structured_content is not None
            result = first
            for _ in range(2):
                payload = result.structured_content
                assert payload is not None
                scope = payload["scope"]
                assert scope["chat_count"] == count
                assert len(scope["chat_ids"]) == min(count, 20)
                assert scope["chat_ids_truncated"] == (count > 20)
                assert scope["pending_chat_ids_truncated"] == (scope["pending_chat_count"] > 20)
                assert len(scope["pending_chat_ids"]) == min(scope["pending_chat_count"], 20)
                assert len(json.dumps(scope, separators=(",", ":")).encode()) < 2048
                assert (
                    len(result.model_dump_json(by_alias=True, exclude_unset=True).encode()) < budget
                )
                assert isinstance(result.content[0], TextContent)
                assert json.loads(result.content[0].text) == payload
                if not payload["next_cursor"]:
                    assert scope["complete"] and scope["pending_chat_count"] == 0
                    break
                result = await client.call_tool(tool, {**args, "cursor": payload["next_cursor"]})
            if tool == "list_chats":
                assert first.structured_content["scope"]["pending_chat_count"] == max(0, count - 30)
            else:
                assert len(first.structured_content["messages"]) == 10
                assert first.structured_content["ordering"] == "global"
