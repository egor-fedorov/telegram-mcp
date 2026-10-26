"""Versioned checkpoint metadata and observations of current message revisions."""

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from telegram_mcp.domain.models import Issue, Message, MessageRef, Model

ExportFormat = Literal["jsonl", "markdown"]
ChangeOutput = Literal["inline", "jsonl", "markdown"]


def content_revision(message: Message, media_identity: str = "") -> str:
    """Ignore delivery slices, chat profiles, counters, reactions and poll votes."""
    data = message.model_dump(
        mode="json",
        include={
            "text",
            "entities",
            "edited_at",
            "content_type",
            "web_page",
            "reply_to",
            "thread_ref",
            "topic_id",
            "forward_origin",
            "attachments",
            "album_id",
            "poll",
        },
        exclude_none=True,
    )
    if isinstance(data.get("poll"), dict):
        data["poll"].pop("results", None)
    data["media_identity"] = media_identity
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SeenMessage(Model):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    available: bool = True


class CheckpointState(Model):
    """No message bodies, names, entities or Telegram objects belong in this record."""

    schema_version: Literal[1] = 1
    kind: Literal["changes", "history"]
    chat_id: str = Field(pattern=r"^-?[1-9]\d*$")
    phase: Literal["baseline", "idle", "recheck", "new", "history", "done"]
    position: int = Field(default=0, ge=0)
    upper_id: int | None = Field(default=None, ge=0)
    date_from: AwareDatetime | None = None
    date_to: AwareDatetime | None = None
    seen: dict[Annotated[int, Field(gt=0)], SeenMessage] = Field(
        default_factory=dict, max_length=100
    )
    recheck_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=100)
    recheck_index: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def consistent_position(self) -> Self:
        phases = (
            {"baseline", "idle", "recheck", "new"}
            if self.kind == "changes"
            else {"history", "done"}
        )
        if self.phase not in phases:
            raise ValueError("Checkpoint phase does not match its kind")
        if self.upper_id is not None and self.position > self.upper_id:
            raise ValueError("Position exceeds the frozen upper ID")
        if self.phase in {"idle", "recheck", "new", "done"} and self.upper_id is None:
            raise ValueError("Checkpoint has no frozen upper ID")
        if (
            self.recheck_index > len(self.recheck_ids)
            or not set(self.recheck_ids) <= self.seen.keys()
        ):
            raise ValueError("Invalid recheck position")
        if self.recheck_ids != sorted(set(self.recheck_ids)) or any(
            mid > self.position for mid in self.seen
        ):
            raise ValueError("Invalid observed message IDs")
        if self.kind == "history" and (self.date_to is None or self.seen or self.recheck_ids):
            raise ValueError("Invalid history checkpoint metadata")
        return self


class Change(Model):
    kind: Literal["new", "updated", "unavailable"]
    message_ref: MessageRef
    revision: str
    observed_at: AwareDatetime
    message: Message | None = None


class ExportFile(Model):
    format: ExportFormat
    name: str
    mime_type: str
    size: int = Field(ge=0)
    sha256: str
    local_path: str
    resource_uri: str
    expires_at: AwareDatetime


class ChangesResult(Model):
    chat_id: str
    changes: list[Change] = Field(default_factory=list)
    returned_count: int = Field(default=0, ge=0)
    omitted_count: int = Field(default=0, ge=0)
    complete: bool = False
    upper_message_id: int | None = None
    recheck_limit: Literal[100] = 100
    rechecked_message_ids: list[int] = Field(default_factory=list)
    tracked_count: int = Field(default=0, ge=0, le=100)
    next_checkpoint: str | None = None
    checkpoint_expires_at: AwareDatetime | None = None
    issues: list[Issue] = Field(default_factory=list)
    export: ExportFile | None = None


class ExportHistoryResult(Model):
    chat_id: str
    exported_count: int = Field(default=0, ge=0)
    omitted_count: int = Field(default=0, ge=0)
    complete: bool = False
    upper_message_id: int | None = None
    date_from: AwareDatetime | None = None
    date_to: AwareDatetime
    next_checkpoint: str | None = None
    checkpoint_expires_at: AwareDatetime | None = None
    issues: list[Issue] = Field(default_factory=list)
    export: ExportFile | None = None
