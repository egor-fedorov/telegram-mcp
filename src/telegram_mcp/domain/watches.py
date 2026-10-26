"""Durable named consumers and their one outstanding delivery."""

import json
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from telegram_mcp.domain.changes import Change, CheckpointState
from telegram_mcp.domain.models import Issue, Model, SavedFile

WatchName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class WatchProgress(Model):
    source_count: int = Field(default=0, ge=0)
    pending_source_count: int = Field(default=0, ge=0)
    sources_this_call: int = Field(default=0, ge=0)
    rpc_pages_this_call: int = Field(default=0, ge=0)


class WatchChangesResult(Model):
    name: WatchName
    batch_id: str | None = None
    changes: list[Change] = Field(default_factory=list, max_length=100)
    returned_count: int = Field(default=0, ge=0)
    omitted_count: int = Field(default=0, ge=0)
    complete: bool = False
    progress: WatchProgress = Field(default_factory=WatchProgress)
    issues: list[Issue] = Field(default_factory=list, max_length=20)
    issue_count: int = Field(default=0, ge=0)
    saved_file: SavedFile | None = None


class PeerRevision(Model):
    top_message_id: int = Field(ge=0)
    pts: int = Field(ge=0)


class WatchPosition(Model):
    sources: dict[str, CheckpointState] = Field(default_factory=dict)
    members: list[str] = Field(default_factory=list)
    remaining: list[str] = Field(default_factory=list)
    cycle_started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    errors: dict[str, Issue] = Field(default_factory=dict)
    revisions: dict[str, PeerRevision] = Field(default_factory=dict)
    probes: dict[str, PeerRevision] = Field(default_factory=dict)
    probe_fallback: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_sources(self) -> Self:
        if not set(self.remaining) <= set(self.members) <= self.sources.keys():
            raise ValueError("Inconsistent watch membership")
        if len(self.remaining) != len(set(self.remaining)) or len(self.members) != len(
            set(self.members)
        ):
            raise ValueError("Duplicate watch source")
        if any(
            key != state.chat_id or state.kind != "changes" for key, state in self.sources.items()
        ):
            raise ValueError("Invalid watch position")
        if (
            not (self.revisions.keys() | self.probes.keys() | set(self.probe_fallback))
            <= self.sources.keys()
        ):
            raise ValueError("Invalid watch revision source")
        return self


class BatchArchive(Model):
    """A reserved permanent-copy location; the receipt exists only after publication."""

    key: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    saved_file: SavedFile | None = None


class PendingDelivery(Model):
    result: WatchChangesResult
    position: WatchPosition
    source_ids: list[str]
    archive: BatchArchive | None = None

    @model_validator(mode="after")
    def consistent_sources(self) -> Self:
        for change in self.result.changes:
            if change.message_ref.chat_id not in self.source_ids or (
                change.message is not None
                and (
                    change.message.message_ref != change.message_ref
                    or change.message.chat.id != change.message_ref.chat_id
                )
            ):
                raise ValueError("Pending content belongs to an unchecked source")
        if any(
            issue.message_ref is not None and issue.message_ref.chat_id not in self.source_ids
            for issue in self.result.issues
            if issue.code == "EXPORT_PROTECTED"
        ):
            raise ValueError("Pending omission belongs to an unchecked source")
        return self


class WatchState(Model):
    schema_version: Literal[1] = 1
    name: WatchName
    chat_ids: list[str] = Field(default_factory=list)
    folder_ids: list[int] = Field(default_factory=list)
    requested_from: AwareDatetime | None = None
    started_at: AwareDatetime
    position: WatchPosition = Field(default_factory=WatchPosition)
    pending: PendingDelivery | None = None
    last_acknowledged: str | None = None

    @model_validator(mode="after")
    def consistent_delivery(self) -> Self:
        if bool(self.chat_ids) == bool(self.folder_ids):
            raise ValueError("Watch requires exactly one selector")
        if self.pending and (
            self.pending.result.name != self.name
            or not self.pending.result.batch_id
            or self.pending.result.saved_file is not None
            or not set(self.pending.source_ids) <= self.pending.position.sources.keys()
        ):
            raise ValueError("Inconsistent pending delivery")
        return self


class WatchInfo(Model):
    name: WatchName
    selector: Literal["chats", "folders"]
    started_at: AwareDatetime
    source_count: int
    folder_ids: list[int]
    completed_at: AwareDatetime | None = None
    pending_batch_id: str | None = None
    pending_change_count: int = 0
    issue_count: int = 0


class WatchList(Model):
    watches: list[WatchInfo]


class WatchAcknowledgement(Model):
    name: WatchName
    batch_id: str
    acknowledged: Literal[True] = True


class WatchDeletion(Model):
    name: WatchName
    deleted: bool


def watch_info(state: WatchState) -> WatchInfo:
    position = state.pending.position if state.pending else state.position
    return WatchInfo(
        name=state.name,
        selector="folders" if state.folder_ids else "chats",
        started_at=state.started_at,
        source_count=len(position.members),
        folder_ids=state.folder_ids,
        completed_at=state.position.completed_at,
        pending_batch_id=state.pending.result.batch_id if state.pending else None,
        pending_change_count=state.pending.result.returned_count if state.pending else 0,
        issue_count=len(position.errors),
    )


def batch_jsonl(name: str, pending: PendingDelivery) -> bytes:
    """Deterministic versioned JSONL of one pending delivery, excluding any archive receipt."""
    result = pending.result
    header = {
        "type": "watch_batch",
        "schema_version": 1,
        "watch": name,
        "batch_id": result.batch_id,
        "source_ids": pending.source_ids,
        "cycle_started_at": pending.position.cycle_started_at.isoformat()
        if pending.position.cycle_started_at
        else None,
        "returned_count": result.returned_count,
        "omitted_count": result.omitted_count,
        "complete": result.complete,
        "issue_count": result.issue_count,
        "progress": result.progress.model_dump(mode="json"),
    }
    records: list[dict[str, object]] = [header]
    records.extend(
        {"type": "change", **change.model_dump(mode="json", exclude_none=True)}
        for change in result.changes
    )
    records.extend(
        {"type": "issue", **issue.model_dump(mode="json", exclude_none=True)}
        for issue in result.issues
    )
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")
