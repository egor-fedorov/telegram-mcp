"""Capabilities for bounded observations, durable positions and transient exports."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from telegram_mcp.domain.changes import CheckpointState, ExportFile, ExportFormat
from telegram_mcp.domain.models import Message
from telegram_mcp.domain.watches import PeerRevision

RECHECK_LIMIT = 100
CHECKPOINT_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_CHECKPOINTS = 4096
MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
MAX_EXPORT_RECORDS = 1000
MAX_EXPORT_BYTES = 20 * 1024 * 1024
MAX_EXPORT_STORE_BYTES = 100 * 1024 * 1024
MAX_EXPORT_FILES = 32
EXPORT_TTL_SECONDS = 900
EXPORT_RESOURCE_PREFIX = "telegram-export://files/"


@dataclass(frozen=True)
class Observation:
    message: Message
    revision: str
    exportable: bool = True


@dataclass(frozen=True)
class ObservationBatch:
    observations: list[Observation]
    next_offset: int
    exhausted: bool


@dataclass(frozen=True)
class PeerState:
    revision: PeerRevision
    exportable: bool


@dataclass(frozen=True)
class CheckpointReceipt:
    token: str
    expires_at: datetime


class IncrementalPort(Protocol):
    async def peer_states(self, chat_ids: list[str]) -> dict[str, PeerState]: ...
    async def checkpoint_binding(self) -> str: ...
    async def recent_observations(
        self, chat_id: str, limit: int, *, before: datetime | None = None
    ) -> list[Observation]: ...
    async def observe_messages(
        self, chat_id: str, message_ids: list[int]
    ) -> dict[int, Observation | None]: ...
    async def read_incremental(
        self,
        chat_id: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
        date_from: datetime | None = None,
    ) -> ObservationBatch: ...
    async def check_export_source(self, chat_id: str) -> None: ...


class CheckpointStorePort(Protocol):
    def get(self, token: str, binding: str, kind: str) -> CheckpointState: ...
    def put(
        self,
        state: CheckpointState,
        binding: str,
        *,
        retain: str | None = None,
        pending: bool = False,
    ) -> CheckpointReceipt: ...
    def confirm(self, token: str) -> None: ...
    def discard(self, token: str) -> None: ...
    def retry_at(self, binding: str) -> datetime | None: ...
    def defer(self, binding: str, until: datetime) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class StoredExport:
    result: ExportFile
    chat_id: str
    binding: str


class ExportStorePort(Protocol):
    async def save(
        self, data: bytes, format: ExportFormat, chat_id: str, binding: str
    ) -> ExportFile: ...
    async def lookup(self, uri: str) -> StoredExport: ...
    async def read(self, uri: str) -> bytes: ...
    async def discard(self, uri: str) -> None: ...
    async def close(self) -> None: ...
