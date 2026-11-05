"""Typed inspection tools work with legacy and modern MCP clients in both schema modes."""

import json

import pytest
from mcp.client import Client
from mcp_types import ElicitResult

from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.version import get_build_info
from tests.support.fakes import msg
from tests.support.outbound import outbound_fixture


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("schema_mode", ["compact", "full"])
async def test_inspection_tools_mirror_text_and_keep_existing_receipt_envelope(
    tmp_path, mode, schema_mode
):
    async def approve(context, params):
        return ElicitResult(action="accept", content={"approve": True})

    async with outbound_fixture(tmp_path) as env:
        env.settings.allowed_chat_ids = env.settings.allowed_write_chat_ids = []
        env.settings.allow_one_off_sends = True
        env.backend.chats[0].username = "recruiter"
        own = msg("1", 10, "Synthetic")
        own.outgoing = True
        env.backend.stored_messages = [own]
        server = create_server(env.service, owns_service=False, schema_mode=schema_mode)
        async with Client(server, mode=mode, elicitation_callback=approve) as client:

            async def call(name, args):
                result = await client.call_tool(name, args)
                assert not result.is_error, result
                assert result.content[0].type == "text"
                assert json.loads(result.content[0].text) == result.structured_content
                return result.structured_content

            resolved = await call("resolve_recipient", {"chat_ref": "@recruiter"})
            assert resolved["chat_id"] == "1" and resolved["confirmation_supported"]
            assert resolved["send_message"]["confirmation_required"]
            receipt = await call(
                "send_message",
                {"chat_ref": resolved["chat_id"], "text": "Synthetic", "idempotency_key": "key"},
            )
            assert await call("get_send_status", {"idempotency_key": "key"}) == {"result": receipt}
            checked = await call("verify_sent_message", {"operation_id": receipt["operation_id"]})
            assert (
                checked["verification"] == "verified"
                and checked["message"]["message_ref"] == "1:10"
            )
            assert "attachments" not in checked["message"]
            journal = await call("list_operations", {"chat_id": "1", "status": "sent"})
            assert journal["operations"][0]["idempotency_key"] == "key"
            assert journal["scope"] == "mcp_journal" and journal["next_cursor"] is None
            status = await call("get_status", {})
            assert status["server"]["version"] == get_build_info()["version"]
            assert "started_at" in status["server"] and status["connect"] is None
