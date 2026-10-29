"""The distributed skill refers to advertised APIs and keeps valid discovery metadata."""

import json
import re

import pytest

from telegram_mcp.adapters.config import Settings
from tests.support.paths import FIXTURES_DIR, PROJECT_ROOT

SKILL = PROJECT_ROOT / "skills" / "telegram-mcp" / "SKILL.md"


def schema_fields(value: object) -> set[str]:
    if isinstance(value, dict):
        fields = set(value.get("properties", {}))
        for child in value.values():
            fields.update(schema_fields(child))
        return fields
    if isinstance(value, list):
        return set().union(*(schema_fields(child) for child in value))
    return set()


def identifiers(text: str) -> set[str]:
    # Inline code uses bare names, calls, assignments or dotted field references.
    # Checking all snake_case names also detects misspelled verbs, e.g. serch_messages.
    return {
        match[0]
        for span in re.findall(r"`([^`\n]+)`", text)
        if (match := re.match(r"[a-z][a-z0-9_]*", span)) and "_" in match[0]
    }


def known_identifiers() -> tuple[set[str], set[str], set[str]]:
    listing = json.loads((FIXTURES_DIR / "mcp_listing.json").read_text())
    tools = {tool["name"] for tool in listing["tools"]}
    prompts = {prompt["name"] for prompt in listing["prompts"]}
    arguments = {arg["name"] for prompt in listing["prompts"] for arg in prompt["arguments"]}
    fields = schema_fields(json.loads((FIXTURES_DIR / "mcp_contract.json").read_text()))
    return tools, prompts, tools | prompts | arguments | fields | set(Settings.model_fields)


def test_skill_discovery_metadata_and_api_references() -> None:
    text = SKILL.read_text()
    _, frontmatter, body = text.split("---", 2)
    # This skill deliberately uses plain, single-line frontmatter scalars.
    metadata = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())
    assert metadata["name"] == SKILL.parent.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert 1 <= len(metadata["name"]) <= 64
    assert 1 <= len(metadata["description"].strip()) <= 1024
    assert metadata["description"] not in {"|", ">"}
    tools, prompts, known = known_identifiers()
    used = identifiers(body)
    assert used & tools and used & prompts
    assert not used - known, f"Unknown skill API or field references: {used - known}"


@pytest.mark.parametrize(
    "old,new", [("search_messages", "serch_messages"), ("period_digest", "period_digset")]
)
def test_unknown_skill_api_references_are_detected(old: str, new: str) -> None:
    text = SKILL.read_text().replace(old, new)
    _, _, known = known_identifiers()
    assert new in identifiers(text) - known
