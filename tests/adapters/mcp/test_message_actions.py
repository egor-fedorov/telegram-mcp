"""The installed-client contract and connect proxy carry real form decisions."""

import sys

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import ElicitResult

from telegram_mcp.adapters.config import save_settings
from telegram_mcp.domain.changes import content_revision
from telegram_mcp.domain.message_actions import ActionPolicy, ActionRule
from tests.support.fakes import FakeBackend, msg
from tests.support.outbound import FakeOutbound
from tests.support.transport import running_server


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_proxy_edit_delete_and_journal_receipts(tmp_path, mode):
    telegram = FakeOutbound()
    draft = msg("1", 11, "Before")
    draft.outgoing = True
    draft.revision = content_revision(draft)
    telegram.action_messages[draft.message_ref.key] = draft
    policy = ActionPolicy(
        rules=(ActionRule(chat_ids=("1",), targets=("own",), actions=("edit", "delete_for_me")),)
    )
    confirmations = []

    async def confirm(context, params):
        confirmations.append(params.message)
        return ElicitResult(action="accept", content={"approve": len(confirmations) == 1})

    async with running_server(
        tmp_path,
        FakeBackend([draft]),
        schema_mode="full",
        outbound_backend=telegram,
        message_actions=policy,
    ) as (settings, _):
        config = tmp_path / "config.toml"
        save_settings(settings, config)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telegram_mcp.cli", "--config", str(config), "connect"],
        )
        async with Client(
            params, mode=mode, elicitation_callback=confirm, read_timeout_seconds=30
        ) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            preview_hint = tools["preview_message_action"].annotations
            edit_hint = tools["edit_message"].annotations
            delete_hint = tools["delete_message"].annotations
            assert preview_hint and preview_hint.read_only_hint
            assert edit_hint and edit_hint.destructive_hint
            assert delete_hint and delete_hint.idempotent_hint
            status = (await client.call_tool("get_status", {})).structured_content
            assert (
                status["message_actions_enabled"]
                and status["message_action_confirmation"] == "per_rule"
            )
            selection = {"message_ref": "1:11"}

            async def preview():
                result = await client.call_tool("preview_message_action", selection)
                assert not result.is_error
                return result.structured_content

            first = await preview()
            assert first["revision"] and first["text_preview"] == "Before"
            result = await client.call_tool(
                "edit_message",
                dict(
                    selection,
                    text="After",
                    idempotency_key="edit",
                    expected_revision=first["revision"],
                ),
            )
            assert result.structured_content["rpc_accepted"]
            second = await preview()
            assert second["revision"] != first["revision"] and second["text_preview"] == "After"
            response = await client.call_tool(
                "delete_message",
                dict(
                    selection,
                    visibility="for_me",
                    expected_revision=second["revision"],
                    idempotency_key="del",
                ),
            )
            receipt = response.structured_content
            assert (
                receipt["status"] == "failed"
                and receipt["error"]["code"] == "CONFIRMATION_DECLINED"
            )
            lookup = await client.call_tool("get_send_status", {"idempotency_key": "del"})
            assert lookup.structured_content["result"] == receipt
    assert len(confirmations) == 2 and len(telegram.action_calls) == 1
