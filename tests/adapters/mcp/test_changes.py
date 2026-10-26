import json

import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp_types import ResourceLink, TextContent, TextResourceContents

from telegram_mcp.adapters.mcp import create_server
from tests.support.fakes import msg
from tests.support.incremental import incremental_fixture


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_checkpoint_tools_have_compact_typed_results_and_text_resources(tmp_path, mode):
    async with incremental_fixture(tmp_path) as env:
        async with Client(
            create_server(env.service, owns_service=False, schema_mode="full"),
            mode=mode,
            cache=None,
        ) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            added_size = sum(
                len(tools[name].model_dump_json()) for name in ("get_changes", "export_history")
            )
            assert added_size < 23000
            for name in ("get_changes", "export_history"):
                assert tools[name].output_schema is not None
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False
                assert annotations.destructive_hint is False
            start = await client.call_tool("get_changes", {"chat_ref": "1"})
            assert not start.is_error and start.structured_content is not None
            assert (
                start.structured_content["complete"] and start.structured_content["changes"] == []
            )
            token = start.structured_content["next_checkpoint"]
            env.backend.stored_messages.append(msg("1", 4, "😀 Original"))
            page = await client.call_tool("get_changes", {"checkpoint": token})
            assert not page.is_error and page.structured_content is not None
            assert isinstance(page.content[0], TextContent)
            assert page.content[0].text == json.dumps(
                page.structured_content, ensure_ascii=False, separators=(",", ":")
            )
            event = page.structured_content["changes"][0]
            assert event["message_ref"] == "1:4" and "chat" not in event["message"]
            assert page.structured_content["chats"]["1"]["type"] == "group"
            exported = await client.call_tool(
                "get_changes", {"checkpoint": token, "output": "jsonl"}
            )
            assert not exported.is_error and exported.structured_content is not None
            assert exported.structured_content["changes"] == [] and len(exported.content) == 2
            assert isinstance(exported.content[1], ResourceLink)
            resource = (await client.read_resource(str(exported.content[1].uri))).contents[0]
            assert isinstance(resource, TextResourceContents)
            assert resource.mime_type == "application/x-ndjson"
            assert json.loads(resource.text.splitlines()[1])["message"]["text"] == "😀 Original"
            history = await client.call_tool(
                "export_history", {"chat_ref": "1", "format": "markdown"}
            )
            assert not history.is_error and history.structured_content is not None
            assert (
                history.structured_content["complete"]
                and history.structured_content["next_checkpoint"] is None
            )
            assert isinstance(history.content[1], ResourceLink)
            uri = str(history.content[1].uri)
            markdown = (await client.read_resource(uri)).contents[0]
            assert isinstance(markdown, TextResourceContents) and "😀 Original" in markdown.text
            assert (await client.list_resources()).resources == []
            env.settings.allowed_chat_ids.clear()
            with pytest.raises(MCPError) as denied:
                await client.read_resource(uri)
            assert denied.value.data["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_checkpoint_errors_use_existing_mcp_error_contract(tmp_path, mode):
    async with incremental_fixture(tmp_path) as env:
        async with Client(
            create_server(env.service, owns_service=False, schema_mode="full"),
            mode=mode,
            cache=None,
        ) as client:
            for name, args, code in (
                ("get_changes", {}, "INVALID_ARGUMENT"),
                ("get_changes", {"checkpoint": "bad"}, "INVALID_CHECKPOINT"),
                ("export_history", {"checkpoint": "a" * 43}, "CHECKPOINT_EXPIRED"),
            ):
                result = await client.call_tool(name, args)
                assert result.is_error and isinstance(result.content[0], TextContent)
                assert code in result.content[0].text
