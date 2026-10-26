"""Consumer-owned ports and backend result records, independent of adapters."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from telegram_mcp.application.attachment_ports import AttachmentPort
from telegram_mcp.application.change_ports import IncrementalPort
from telegram_mcp.application.requests import MessageRead, SearchRead
from telegram_mcp.domain.models import (
    Chat,
    Folder,
    ForumTopic,
    Issue,
    Message,
    MessageRef,
    PublicChannel,
)
from telegram_mcp.domain.policy import ReadPolicy


@dataclass(frozen=True)
class SearchAnchor:
    """A completed date-boundary lookup before the first filtered search page."""

    offset_id: int


type MessageOffset = int | SearchAnchor


@dataclass
class Batch:
    messages: list[Message]
    next_offset: MessageOffset
    exhausted: bool
    issues: list[Issue] = field(default_factory=list)


@dataclass(frozen=True)
class GlobalOffset:
    rate: int = 0
    peer_id: str | None = None
    message_id: int = 0
    access_hash: int | None = None


@dataclass
class GlobalBatch:
    messages: list[Message]
    next_offset: GlobalOffset
    exhausted: bool
    issues: list[Issue] = field(default_factory=list)


@dataclass
class HistoryWindow:
    messages: list[Message]
    scanned_count: int
    exhausted: bool
    issues: list[Issue] = field(default_factory=list)


@dataclass
class DiscoveryBatch:
    channels: list[PublicChannel]
    telegram_total_count: int | None = None


@dataclass(frozen=True)
class TopicOffset:
    date: datetime | None = None
    message_id: int = 0
    topic_id: int = 0


@dataclass
class TopicBatch:
    topics: list[ForumTopic]
    next_offset: TopicOffset | None
    exhausted: bool
    order_by_create_date: bool
    telegram_total_count: int
    issues: list[Issue] = field(default_factory=list)


class Clock(Protocol):
    def utc_now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class PolicyProvider(Protocol):
    def current(self) -> ReadPolicy: ...


class CursorPort(Protocol):
    def put(self, kind: str, fingerprint: str, value: object) -> str: ...
    def get(self, token: str, kind: str, fingerprint: str) -> object: ...


@dataclass(frozen=True)
class ExecutionLimits:
    rpc_pages_per_call: int = 12
    cursor_ttl_seconds: int = 900
    max_cursors: int = 128
    page_deadline_seconds: int = 35
    page_size: int = 30
    text_limit: int = 12000
    text_budget: int = 100000


class RuntimePort(Protocol):
    async def status(self) -> dict[str, object]: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class FolderName:
    id: int
    title: str


class ChatPort(Protocol):
    async def check_authorization(self) -> None: ...
    async def catalog(self) -> tuple[list[Chat], list[Folder]]: ...
    async def folder_names(self) -> list[FolderName]:
        """Fresh folder IDs and titles without reading the dialog catalog."""
        ...

    async def resolve_chat(self, ref: str, *, refresh: bool = False) -> Chat: ...
    async def public_channel(self, chat_id: str) -> Chat: ...
    async def chat_details(self, chat: Chat) -> dict[str, object]: ...


class DiscoveryPort(Protocol):
    async def search_public_channels(self, query: str, limit: int) -> DiscoveryBatch: ...
    async def get_similar_channels(self, chat_ref: str) -> DiscoveryBatch: ...


class ForumPort(Protocol):
    async def fetch_topics(
        self, chat_id: str, *, offset: TopicOffset, limit: int = 30, query: str | None = None
    ) -> TopicBatch: ...
    async def get_topic(self, chat_id: str, topic_id: int) -> ForumTopic: ...


class MessagePort(Protocol):
    async def search_global(
        self, request: SearchRead, *, offset: GlobalOffset, limit: int, chat_ids: list[str]
    ) -> GlobalBatch: ...
    async def resolve_message(self, ref: str | MessageRef) -> MessageRef: ...
    async def message(self, ref: MessageRef) -> Message | None: ...
    async def messages(self, chat_id: str, message_ids: list[int]) -> dict[int, Message | None]: ...
    async def history_window(
        self, chat_id: str, *, anchor_id: int, limit: int, newer: bool
    ) -> HistoryWindow: ...
    async def discussion_chat(self, chat: Chat) -> str: ...
    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef: ...
    async def read(
        self, chat_id: str, request: MessageRead, *, offset: MessageOffset = 0, limit: int = 30
    ) -> Batch: ...


class Backend(
    ChatPort,
    DiscoveryPort,
    ForumPort,
    MessagePort,
    AttachmentPort,
    IncrementalPort,
    RuntimePort,
    Protocol,
):
    """Aggregate accepted only at application assembly, not by individual use cases."""
