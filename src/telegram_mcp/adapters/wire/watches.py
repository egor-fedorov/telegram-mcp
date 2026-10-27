"""Named subscriptions and their batches."""

from typing import Annotated, Literal, NotRequired

from pydantic import AwareDatetime, Field, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.changes import Change
from telegram_mcp.adapters.wire.common import CONFIG, ChatID, ChatSummary, Count, Issue, SavedFile


@with_config(CONFIG)
class WatchInfo(TypedDict):
    name: str
    selector: Literal["chats", "folders"]
    started_at: AwareDatetime
    source_count: Count
    folder_ids: list[int]
    completed_at: NotRequired[AwareDatetime]
    pending_batch_id: NotRequired[str]
    pending_change_count: Count
    issue_count: Count


@with_config(CONFIG)
class WatchList(TypedDict):
    watches: list[WatchInfo]


@with_config(CONFIG)
class WatchProgress(TypedDict):
    source_count: Count
    pending_source_count: Count
    sources_this_call: Count
    rpc_pages_this_call: Count


@with_config(CONFIG)
class WatchChangesResult(TypedDict):
    chats: dict[ChatID, ChatSummary]
    name: str
    batch_id: str | None
    changes: list[Change]
    returned_count: Count
    omitted_count: Count
    complete: bool
    progress: WatchProgress
    issues: Annotated[list[Issue], Field(max_length=20)]
    issue_count: Count
    saved_file: NotRequired[SavedFile]


@with_config(CONFIG)
class WatchAcknowledgement(TypedDict):
    name: str
    batch_id: str
    acknowledged: Literal[True]


@with_config(CONFIG)
class WatchDeletion(TypedDict):
    name: str
    deleted: bool
