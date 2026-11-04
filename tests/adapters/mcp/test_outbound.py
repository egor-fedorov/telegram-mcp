"""MCP-native confirmation, capability reporting, and non-blocking human waits."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.client import Client
from mcp.server.mcpserver.context import Context
from mcp_types import ClientCapabilities, ElicitResult

from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.mcp.confirmation import ConfirmationRounds, FormRequired
from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.domain.models import ToolError
from tests.support.outbound import outbound_fixture


@pytest.mark.parametrize("changed", ["description", "expired", "unsolicited", "reused"])
async def test_confirmation_rounds_reject_invalid_or_reused_approval(monkeypatch, changed):
    context = SimpleNamespace(
        client_capabilities=ClientCapabilities.model_validate({"elicitation": {"form": {}}}),
        protocol_version="2026-07-28",
        request_state=None,
        input_responses=None,
    )
    rounds = ConfirmationRounds()
    ask = rounds.confirmation(cast(Context[None, Any], context), ToolCallGate())
    assert ask is not None
    monkeypatch.setattr("telegram_mcp.adapters.mcp.confirmation.time.monotonic", lambda: 1000)
    with pytest.raises(FormRequired) as challenge:
        await ask("Send exact text; operation one")
    context.request_state = challenge.value.result.request_state
    context.input_responses = {"approve": ElicitResult(action="accept", content={"approve": True})}
    if changed == "expired":
        monkeypatch.setattr("telegram_mcp.adapters.mcp.confirmation.time.monotonic", lambda: 1121)
    elif changed == "unsolicited":
        context.request_state = None
    elif changed == "reused":
        assert await ask("Send exact text; operation one")
    with pytest.raises(ToolError, match="CONFIRMATION_(EXPIRED|INVALID)"):
        await ask(
            "Send different text" if changed == "description" else "Send exact text; operation one"
        )


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_confirmed_send_has_typed_receipt_and_bound_form(tmp_path, mode):
    forms = []

    async def confirm(context, params):
        forms.append(params)
        return ElicitResult(action="accept", content={"approve": True})

    async with outbound_fixture(tmp_path) as env:
        server = create_server(env.service, owns_service=False, schema_mode="full")
        async with Client(server, mode=mode, elicitation_callback=confirm) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert len(tools) == 33
            assert tools["send_message"].annotations is not None
            assert not tools["send_message"].annotations.read_only_hint
            assert tools["send_message"].annotations.idempotent_hint
            assert "ctx" not in tools["send_message"].input_schema["properties"]
            status = await client.call_tool("get_status", {})
            assert status.structured_content["confirmation_supported"] is True
            result = await client.call_tool(
                "send_message", {"chat_ref": "1", "text": "Approved text", "idempotency_key": "one"}
            )
            assert not result.is_error, result
            assert result.structured_content["status"] == "sent"
            assert result.structured_content["message_ref"] == "1:10"
            assert (
                len(forms) == 1
                and "Approved text" in forms[0].message
                and "(1)" in forms[0].message
            )
            again = await client.call_tool(
                "send_message", {"chat_ref": "1", "text": "Approved text", "idempotency_key": "one"}
            )
            assert again.structured_content == result.structured_content and len(forms) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("answer", ["unsupported", "decline", "cancel", "false"])
async def test_client_must_explicitly_approve(tmp_path, mode, answer):
    async def confirm(context, params):
        return ElicitResult(
            action="accept" if answer == "false" else answer,
            content={"approve": False} if answer == "false" else None,
        )

    async with outbound_fixture(tmp_path) as env:
        server = create_server(env.service, owns_service=False)
        async with Client(
            server, mode=mode, elicitation_callback=None if answer == "unsupported" else confirm
        ) as client:
            result = await client.call_tool(
                "send_message", {"chat_ref": "1", "text": "No send", "idempotency_key": "key"}
            )
            assert not result.is_error
            assert result.structured_content["status"] == "failed"
            assert result.structured_content["error"]["code"].startswith("CONFIRMATION_")
            assert not env.telegram.send_calls


async def test_confirmation_releases_tool_capacity_for_reads(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def confirm(context, params):
        entered.set()
        await release.wait()
        return ElicitResult(action="accept", content={"approve": True})

    async with outbound_fixture(tmp_path) as env:
        server = create_server(env.service, owns_service=False, gate=ToolCallGate(capacity=1))
        async with Client(server, mode="legacy", elicitation_callback=confirm) as client:
            task = asyncio.create_task(
                client.call_tool(
                    "send_message", {"chat_ref": "1", "text": "One", "idempotency_key": "key"}
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            status = await asyncio.wait_for(client.call_tool("get_status", {}), 2)
            assert status.structured_content["authorized"] is True
            release.set()
            assert (await task).structured_content["status"] == "sent"
