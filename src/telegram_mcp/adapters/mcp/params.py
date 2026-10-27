"""Shared MCP parameter types and tool annotations."""

from typing import Annotated

from mcp_types import ToolAnnotations
from pydantic import Field

Limit = Annotated[int, Field(ge=1, le=100)]
ChatID = Annotated[str, Field(pattern=r"^-?[1-9]\d*$")]
TopicID = Annotated[int, Field(gt=0, le=2147483647, strict=True)]
FolderIDs = Annotated[list[int], Field(min_length=1, max_length=100)]
ChatIDs = Annotated[list[ChatID], Field(min_length=1, max_length=1000)]
Budget = Annotated[int, Field(ge=1, le=1000, strict=True)]

READONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
PERSISTENT_READ = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
