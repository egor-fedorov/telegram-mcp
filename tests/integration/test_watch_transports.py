import asyncio
import json
from pathlib import Path

import pytest

from tests.support.fakes import BASE
from tests.support.transport import connection, running_server

# Real transports and subprocesses; excluded from make test-fast.
pytestmark = pytest.mark.slow


async def exercise(kind, path, mode, schema_mode, settings=None):
    async with connection(kind, path, mode, schema_mode, settings) as client:
        tools = (await client.list_tools()).tools
        assert all((tool.output_schema is None) == (schema_mode == "compact") for tool in tools)
        created = await client.call_tool(
            "create_watch", {"name": "jobs", "chat_refs": ["1"], "date_from": BASE.isoformat()}
        )
        assert not created.is_error
        first = await client.call_tool("get_watch_changes", {"name": "jobs", "limit": 1})
        assert not first.is_error and first.structured_content
        batch = first.structured_content
        assert [item["message_ref"] for item in batch["changes"]] == ["1:1"]
    # STDIO restarts the owning process; HTTP/connect replace only the client.
    async with connection(kind, path, mode, schema_mode, settings) as client:
        found = await client.call_tool("list_watches", {})
        assert found.structured_content["watches"][0]["pending_batch_id"] == batch["batch_id"]
        replay = await client.call_tool("get_watch_changes", {"name": "jobs"})
        assert replay.structured_content == batch
        second = await client.call_tool(
            "get_watch_changes", {"name": "jobs", "limit": 1, "ack_batch_id": batch["batch_id"]}
        )
        assert not second.is_error
        assert second.structured_content["changes"][0]["message_ref"] == "1:2"
        retry = await client.call_tool(
            "get_watch_changes", {"name": "jobs", "ack_batch_id": batch["batch_id"]}
        )
        assert retry.structured_content == second.structured_content
        ack = await client.call_tool(
            "ack_watch_changes", {"name": "jobs", "batch_id": second.structured_content["batch_id"]}
        )
        assert not ack.is_error
        exported = await client.call_tool("export_history", {"chat_ref": "1", "save": True})
        assert not exported.is_error
        saved = Path(exported.structured_content["export"]["saved_file"]["local_path"])
        metadata_path = Path(exported.structured_content["export"]["saved_file"]["metadata_path"])
        metadata = json.loads(await asyncio.to_thread(metadata_path.read_text, encoding="utf-8"))
        assert metadata["source"]["chat_id"] == "1"
        assert metadata["source"]["tool"] == "export_history"
    return saved


@pytest.mark.parametrize("schema_mode", ["compact", "full"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("kind", ["stdio", "http", "connect"])
async def test_monitoring_restart_and_archives_across_transports(tmp_path, schema_mode, mode, kind):
    async with asyncio.timeout(60):
        if kind == "stdio":
            saved = await exercise(kind, tmp_path, mode, schema_mode)
        else:
            async with running_server(tmp_path, schema_mode=schema_mode, archive=True) as (
                settings,
                _,
            ):
                saved = await exercise(kind, tmp_path, mode, schema_mode, settings)
    assert await asyncio.to_thread(saved.exists)
