"""Add the executing proxy's identity without confusing it with the upstream build."""

import json
from typing import Any

from mcp_types import CallToolResult, TextContent, Tool

from telegram_mcp.adapters.wire.status import BuildInfo

BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "string"},
        "build_commit": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["version", "build_commit"],
}


def status_tool(tool: Tool) -> Tool:
    if tool.name != "get_status" or tool.output_schema is None:
        return tool
    schema = dict(tool.output_schema)
    properties = dict(schema.get("properties", {}))
    if "connect" in properties and "server" in properties:
        return tool
    properties["connect"] = {"anyOf": [BUILD_SCHEMA, {"type": "null"}]}
    # Older servers have no build fields. Keep their version from discovery/initialization.
    properties.setdefault("server", BUILD_SCHEMA)
    schema["properties"] = properties
    return tool.model_copy(update={"output_schema": schema})


def status_result(
    result: CallToolResult, connect: BuildInfo, upstream_version: str | None
) -> CallToolResult:
    if result.is_error:
        return result
    payload: Any = result.structured_content
    if payload is None:
        if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
            return result
        try:
            payload = json.loads(result.content[0].text)
        except ValueError:
            return result
    if not isinstance(payload, dict) or "authorized" not in payload:
        return result
    payload = {**payload, "connect": connect}
    if "server" not in payload and upstream_version is not None:
        payload["server"] = {"version": upstream_version, "build_commit": None}
    return result.model_copy(
        update={
            "structured_content": payload,
            "content": [
                TextContent(
                    type="text", text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
            ],
        }
    )
