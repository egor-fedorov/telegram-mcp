"""Shared response pieces: chats, issues, scopes and saved files."""

from typing import Annotated, Any, NotRequired

from pydantic import AwareDatetime, ConfigDict, Field, with_config
from typing_extensions import TypedDict

from telegram_mcp.domain import models


def _remove_titles(schema: dict[str, Any]) -> None:
    """Remove presentation labels without changing constraints or local references."""
    schema.pop("title", None)
    for key, value in schema.items():
        if key in {"properties", "$defs", "patternProperties"}:
            for child in value.values():
                _remove_titles(child)
        elif isinstance(value, dict):
            _remove_titles(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _remove_titles(item)


CONFIG = ConfigDict(extra="forbid", json_schema_extra=_remove_titles)
Count = Annotated[int, Field(ge=0)]
ChatID = Annotated[str, Field(pattern=r"^-?[1-9]\d*$")]
MessageKey = Annotated[str, Field(pattern=r"^-?[1-9]\d*:[1-9]\d*$")]
TopicID = Annotated[int, Field(gt=0, le=2147483647)]


@with_config(CONFIG)
class ChatSummary(TypedDict):
    title: str
    type: models.ChatType
    username: NotRequired[str]


@with_config(CONFIG)
class ChatMetadata(ChatSummary):
    is_contact: bool
    is_muted: bool
    is_archived: bool
    is_supergroup: bool
    is_forum: bool
    is_community: bool
    unread_count: int
    unread_mentions_count: int
    marked_unread: bool
    matched_folder_ids: list[int]
    pinned_in_folder_ids: list[int]


@with_config(CONFIG)
class Chat(ChatMetadata):
    id: ChatID


@with_config(CONFIG)
class Issue(TypedDict):
    code: str
    message: str
    chat_id: NotRequired[ChatID]
    message_ref: NotRequired[MessageKey]
    retry_after_seconds: NotRequired[int]
    input_index: NotRequired[Count]


@with_config(CONFIG)
class Scope(TypedDict):
    folder_ids: list[int]
    chat_ids: Annotated[list[ChatID], Field(max_length=20)]
    chat_count: Count
    chat_ids_truncated: bool
    resolved_at: AwareDatetime
    pending_chat_ids: Annotated[list[ChatID], Field(max_length=20)]
    pending_chat_count: Count
    pending_chat_ids_truncated: bool
    complete: bool


@with_config(CONFIG)
class SavedFile(TypedDict):
    local_path: str
    metadata_path: str
    size: Count
    sha256: str
    saved_at: AwareDatetime
