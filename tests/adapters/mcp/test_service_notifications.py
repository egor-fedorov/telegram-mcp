"""Exercise access checks through MCP and the real Telegram reference/message adapters."""

import json
from unittest.mock import AsyncMock

import pytest
from mcp.client import Client
from telethon import types

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.telegram import TelegramBackend, TelegramRuntime
from telegram_mcp.bootstrap import build_service
from tests.support.fakes import BASE


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("tool", ["get_messages", "get_context"])
@pytest.mark.parametrize(
    "reference",
    [
        "77700\u0660:1",
        {"chat_id": "77700\u0660", "message_id": 1},
        "77700\uff10:1",
        {"chat_id": "77700\uff10", "message_id": 1},
    ],
    ids=["arabic-string", "arabic-object", "fullwidth-string", "fullwidth-object"],
)
async def test_unicode_chat_id_cannot_bypass_service_notification_policy(
    tmp_path, mode, tool, reference
) -> None:
    settings = Settings(session_path=tmp_path / "account.session", allow_all_chats=True)
    telegram = AsyncMock()
    telegram.is_connected = lambda: True
    telegram.get_entity.return_value = types.User(id=777000, first_name="Telegram")
    telegram.get_messages.return_value = [
        types.Message(
            id=1,
            peer_id=types.PeerUser(777000),
            date=BASE,
            message="SYNTHETIC_SECURITY_SENTINEL",
        )
    ]
    backend = TelegramBackend(settings, runtime=TelegramRuntime(settings, client=telegram))
    async with Client(create_server(build_service(backend, settings)), mode=mode) as client:
        # The canonical ID remains denied; Unicode aliases must fail before any entity/message RPC.
        canonical = await client.call_tool("get_messages", {"refs": ["777000:1"]})
        assert canonical.structured_content is not None
        assert canonical.structured_content["messages"] == []
        assert canonical.structured_content["issues"][0]["code"] == "ACCESS_DENIED"

        arguments = (
            {"refs": [reference]}
            if tool == "get_messages"
            else {"ref": reference, "before": 0, "after": 0, "reply_depth": 0}
        )
        result = await client.call_tool(tool, arguments)
        serialized = result.model_dump_json()
        assert "SYNTHETIC_SECURITY_SENTINEL" not in serialized
        if tool == "get_messages":
            assert not result.is_error and result.structured_content is not None
            data = result.structured_content
            assert data["messages"] == []
            assert data["issues"][0]["code"] == "INVALID_REFERENCE"
            assert data["issues"][0]["input_index"] == 0
        else:
            assert result.is_error
            text = "".join(block.text for block in result.content if block.type == "text")
            assert json.loads(text[text.index("{") :])["code"] == "INVALID_REFERENCE"
        telegram.get_entity.assert_not_awaited()
        telegram.get_messages.assert_not_awaited()
