"""Compare the public schemas against the pre-refactor MCP contract."""

import json
from pathlib import Path

import pytest
from mcp.client import Client

from telegram_mcp.config import Settings
from telegram_mcp.server import create_server
from telegram_mcp.service import Service
from tests.fakes import FakeBackend


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_contract_is_unchanged(mode: str) -> None:
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
        expected = json.loads((Path(__file__).parent / "fixtures/mcp_contract.json").read_text())
        assert {"definitions": definitions, "tools": tools} == expected
