import json

import pytest
from mcp.client import Client

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from tests.support.fakes import BASE
from tests.support.incremental import incremental_fixture


@pytest.mark.parametrize("schema_mode", ["compact", "full"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_named_delivery_and_discovery_across_client_sessions(tmp_path, mode, schema_mode):
    async with incremental_fixture(tmp_path) as env:
        server = create_server(env.service, owns_service=False, schema_mode=schema_mode)
        async with Client(server, mode=mode) as client:
            listing = await client.list_tools()
            assert len(listing.tools) == 22
            assert all(
                (tool.output_schema is None) == (schema_mode == "compact") for tool in listing.tools
            )
            if schema_mode == "compact":
                assert server.instructions is not None
                assert (
                    len(listing.model_dump_json(by_alias=True, exclude_none=True).encode())
                    < 32 * 1024
                )
                assert len(server.instructions.encode()) <= 1024
            created = await client.call_tool(
                "create_watch", {"name": "jobs", "chat_refs": ["1"], "date_from": BASE.isoformat()}
            )
            assert not created.is_error
            page = await client.call_tool("get_watch_changes", {"name": "jobs", "limit": 2})
            assert not page.is_error and page.structured_content is not None
            payload = page.structured_content
            assert payload["chats"]["1"]["title"] == "Chat 1"
            assert payload["changes"][0]["message_ref"] == "1:1"
            assert "chat" not in payload["changes"][0]["message"]
            assert page.content[0].type == "text"
            assert json.loads(page.content[0].text) == payload
        # A separate client does not need any per-chat checkpoint from the first.
        async with Client(
            create_server(env.service, owns_service=False, schema_mode=schema_mode), mode=mode
        ) as client:
            listing = await client.call_tool("list_watches", {})
            assert (
                listing.structured_content["watches"][0]["pending_batch_id"] == payload["batch_id"]
            )
            replay = await client.call_tool("get_watch_changes", {"name": "jobs"})
            assert replay.structured_content == payload
            ack = await client.call_tool(
                "ack_watch_changes", {"name": "jobs", "batch_id": payload["batch_id"]}
            )
            assert not ack.is_error
            deleted = await client.call_tool("delete_watch", {"name": "jobs"})
            assert deleted.structured_content["deleted"] is True


@pytest.mark.parametrize("schema_mode", ["compact", "full"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_server_validates_results_even_when_schemas_are_not_advertised(
    tmp_path, monkeypatch, schema_mode, mode
):
    async with incremental_fixture(tmp_path) as env:

        async def invalid():
            return {"authorized": "invalid", "connected": True}

        monkeypatch.setattr(env.service, "get_status", invalid)
        async with Client(
            create_server(env.service, owns_service=False, schema_mode=schema_mode), mode=mode
        ) as client:
            result = await client.call_tool("get_status", {})
            assert result.is_error
            assert result.structured_content is None


@pytest.mark.parametrize("schema_mode", ["compact", "full"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_saved_batch_receipt_is_typed_and_required_before_ack(tmp_path, mode, schema_mode):
    settings = Settings(allowed_chat_ids=["1"], archive_directory=tmp_path / "archive")
    async with incremental_fixture(tmp_path, settings=settings) as env:
        server = create_server(env.service, owns_service=False, schema_mode=schema_mode)
        async with Client(server, mode=mode) as client:
            await client.call_tool(
                "create_watch", {"name": "jobs", "chat_refs": ["1"], "date_from": BASE.isoformat()}
            )
            page = await client.call_tool("get_watch_changes", {"name": "jobs", "save": True})
            payload = page.structured_content
            assert not page.is_error and payload is not None
            assert set(payload["saved_file"]) == {
                "local_path",
                "metadata_path",
                "size",
                "sha256",
                "saved_at",
            }
            assert page.content[0].type == "text"
            assert json.loads(page.content[0].text) == payload
            ack = await client.call_tool(
                "ack_watch_changes", {"name": "jobs", "batch_id": payload["batch_id"]}
            )
            assert not ack.is_error
