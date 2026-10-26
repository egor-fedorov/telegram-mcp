"""Compare the self-contained public schemas against the versioned MCP contract."""

import json
from pathlib import Path

import pytest
from mcp.client import Client

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service as Service
from tests.fakes import FakeBackend


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_contract_matches_versioned_fixture(mode: str) -> None:
    async with Client(create_server(Service(FakeBackend(), Settings())), mode=mode) as client:
        listing = await client.list_tools()
        definitions = {}
        tools = {}
        for tool in listing.tools:
            entry = {
                "input": dict(tool.input_schema),
                "output": dict(tool.output_schema or {}),
                "annotations": tool.annotations.model_dump(mode="json")
                if tool.annotations
                else None,
            }
            for schema in (entry["input"], entry["output"]):
                for name, value in schema.pop("$defs", {}).items():
                    if name in definitions:
                        assert definitions[name] == value
                    definitions[name] = value
            tools[tool.name] = entry
        expected = json.loads(
            (Path(__file__).parent / "fixtures/mcp_contract.json").read_text(encoding="utf-8")
        )
        assert {"definitions": definitions, "tools": tools} == expected
