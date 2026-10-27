"""Checkpointed changes and history exports."""

from typing import Annotated, Literal, NotRequired

from pydantic import AwareDatetime, Field, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import (
    CONFIG,
    ChatID,
    ChatSummary,
    Count,
    Issue,
    MessageKey,
    SavedFile,
)
from telegram_mcp.adapters.wire.messages import Message
from telegram_mcp.domain import changes


@with_config(CONFIG)
class ExportFile(TypedDict):
    format: changes.ExportFormat
    name: str
    mime_type: str
    size: Count
    sha256: str
    local_path: str
    resource_uri: str
    expires_at: AwareDatetime
    saved_file: NotRequired[SavedFile]


@with_config(CONFIG)
class Change(TypedDict):
    kind: Literal["new", "updated", "unavailable"]
    message_ref: MessageKey
    revision: str
    observed_at: AwareDatetime
    message: NotRequired[Message]


@with_config(CONFIG)
class ChangesResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    chat_id: ChatID
    baseline_created: bool
    changes: list[Change]
    returned_count: Count
    omitted_count: Count
    complete: bool
    upper_message_id: NotRequired[Count]
    recheck_limit: Literal[100]
    rechecked_message_ids: list[int]
    tracked_count: Annotated[int, Field(ge=0, le=100)]
    next_checkpoint: str | None
    checkpoint_expires_at: NotRequired[AwareDatetime]
    issues: list[Issue]
    export: NotRequired[ExportFile]


@with_config(CONFIG)
class ExportHistoryResult(TypedDict):
    chat_id: ChatID
    exported_count: Count
    omitted_count: Count
    complete: bool
    upper_message_id: NotRequired[Count]
    date_from: NotRequired[AwareDatetime]
    date_to: AwareDatetime
    next_checkpoint: str | None
    checkpoint_expires_at: NotRequired[AwareDatetime]
    issues: list[Issue]
    export: NotRequired[ExportFile]
