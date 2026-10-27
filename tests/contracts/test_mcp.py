"""Compare the self-contained public schemas against the versioned MCP contract."""

import inspect
import json

import pytest
from mcp.client import Client

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.bootstrap import build_service as Service
from tests.support.fakes import FakeBackend
from tests.support.paths import FIXTURES_DIR


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_contract_matches_versioned_fixture(mode: str) -> None:
    async with Client(
        create_server(Service(FakeBackend(), Settings()), schema_mode="full"), mode=mode
    ) as client:
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
        expected = json.loads((FIXTURES_DIR / "mcp_contract.json").read_text(encoding="utf-8"))
        assert {"definitions": definitions, "tools": tools} == expected


async def advertised_listing(mode: str) -> dict[str, object]:
    """Ordered names and descriptions that the schema contract does not cover.

    Python 3.13 strips docstring indentation at compile time, so descriptions are
    compared after the same cleanup on every supported version.
    """
    async with Client(create_server(Service(FakeBackend(), Settings())), mode=mode) as client:
        tools = (await client.list_tools()).tools
        prompts = (await client.list_prompts()).prompts
        templates = (await client.list_resource_templates()).resource_templates
        return {
            "instructions": client.instructions,
            "tools": [
                {"name": tool.name, "description": inspect.cleandoc(tool.description or "")}
                for tool in tools
            ],
            "prompts": [prompt.model_dump(mode="json", exclude_none=True) for prompt in prompts],
            "resource_templates": [
                template.model_dump(mode="json", exclude_none=True) for template in templates
            ],
        }


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_advertised_order_and_descriptions_match_fixture(mode: str) -> None:
    expected = json.loads((FIXTURES_DIR / "mcp_listing.json").read_text(encoding="utf-8"))
    assert await advertised_listing(mode) == expected
