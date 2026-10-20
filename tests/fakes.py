from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from pydantic import ValidationError

from telegram_mcp.backend import Batch, DiscoveryBatch, TopicBatch, TopicOffset
from telegram_mcp.models import (
    Chat,
    ContentType,
    Folder,
    ForumTopic,
    Message,
    MessageRef,
    ToolError,
)

BASE = datetime(2026, 9, 1, tzinfo=UTC)


def msg(
    cid: str,
    mid: int,
    text: str = "budget discussion",
    *,
    minute: int | None = None,
    content: ContentType = "text",
    reply_to: MessageRef | None = None,
    topic_id: int | None = None,
) -> Message:
    return Message(
        message_ref=MessageRef(chat_id=cid, message_id=mid),
        chat=Chat(id=cid, title=f"Chat {cid}", type="group"),
        sent_at=BASE + timedelta(minutes=mid if minute is None else minute),
        text=text,
        content_type=content,
        reply_to=reply_to,
        topic_id=topic_id,
    )


class FakeBackend:
    def __init__(
        self,
        messages: list[Message] | None = None,
        chats: list[Chat] | None = None,
        folders: list[Folder] | None = None,
        external_chats: list[Chat] | None = None,
    ):
        self.stored_messages = messages or []
        self.chats = (
            chats
            if chats is not None
            else list({m.chat.id: m.chat for m in self.stored_messages}.values())
        )
        self.external_chats = external_chats or []
        self.folders = folders or []
        self.reads: list[str] = []
        self.fetches: list[str] = []
        self.faults: dict[str, ToolError] = {}
        self.catalog_fault: ToolError | None = None
        self.public_faults: dict[str, ToolError] = {}
        self.message_faults: dict[str, ToolError] = {}
        self.public_checks: list[str] = []
        self.resolve_calls: list[tuple[str, bool]] = []
        self.batch_reads: list[tuple[str, list[int]]] = []
        self.fetch_offsets: list[tuple[str, int]] = []
        self.closed = False
        self.thread_calls = 0
        self.backend_calls: list[str] = []
        self.discovery_search_batch = DiscoveryBatch(channels=[])
        self.discovery_similar_batch = DiscoveryBatch(channels=[])
        self.discovery_search_calls: list[tuple[str, int]] = []
        self.discovery_similar_calls: list[str] = []
        self.discovery_faults: dict[str, ToolError] = {}
        self.topic_batches: dict[tuple[str, TopicOffset], TopicBatch] = {}
        self.topic_faults: dict[str, ToolError] = {}
        self.topic_fetches: list[tuple[str, TopicOffset, int, str | None]] = []
        self.topics: dict[tuple[str, int], ForumTopic] = {}
        self.topic_lookup_faults: dict[tuple[str, int], ToolError] = {}
        self.topic_lookups: list[tuple[str, int]] = []
        self.pinned_message_keys: set[str] = set()
        self.fetch_pinned_flags: list[bool] = []
        self.fetch_topic_ids: list[int | None] = []

    async def status(self) -> dict[str, object]:
        self.backend_calls.append("status")
        return {"authorized": True, "connected": True}

    async def close(self) -> None:
        self.backend_calls.append("close")
        self.closed = True

    async def catalog(self) -> tuple[list[Chat], list[Folder]]:
        self.backend_calls.append("catalog")
        if self.catalog_fault:
            raise self.catalog_fault
        return list(self.chats), list(self.folders)

    async def resolve_chat(self, ref: str, *, refresh: bool = False) -> Chat:
        self.backend_calls.append("resolve_chat")
        self.resolve_calls.append((ref, refresh))
        normalized = ref.removeprefix("@").removeprefix("https://t.me/").rstrip("/")
        for chat in self.chats + self.external_chats:
            if normalized in (chat.id, chat.username):
                return chat
        raise ToolError("NOT_FOUND", "Unknown chat")

    async def public_channel(self, chat_id: str) -> Chat:
        self.backend_calls.append("public_channel")
        self.public_checks.append(chat_id)
        if chat_id in self.public_faults:
            raise self.public_faults[chat_id]
        chat = await self.resolve_chat(chat_id, refresh=True)
        if chat.type != "channel" or not chat.username:
            raise ToolError("ACCESS_DENIED", "Chat is not a public broadcast channel.")
        return chat

    async def chat_details(self, chat: Chat) -> dict[str, object]:
        self.backend_calls.append("chat_details")
        return {"description": "Example", "server_can_send": False}

    async def search_public_channels(self, query: str, limit: int) -> DiscoveryBatch:
        self.backend_calls.append("search_public_channels")
        self.discovery_search_calls.append((query, limit))
        if "search" in self.discovery_faults:
            raise self.discovery_faults["search"]
        return self.discovery_search_batch

    async def get_similar_channels(self, chat_ref: str) -> DiscoveryBatch:
        self.backend_calls.append("get_similar_channels")
        self.discovery_similar_calls.append(chat_ref)
        if "similar" in self.discovery_faults:
            raise self.discovery_faults["similar"]
        return self.discovery_similar_batch

    async def resolve_message(self, ref: str | MessageRef) -> MessageRef:
        self.backend_calls.append("resolve_message")
        if isinstance(ref, MessageRef):
            return ref
        try:
            if ref.startswith("https://t.me/"):
                username, mid = urlparse(ref).path.strip("/").split("/")
                cid = (await self.resolve_chat(username)).id
            else:
                cid, mid = ref.split(":")
            return MessageRef(chat_id=cid, message_id=int(mid))
        except (ValueError, ValidationError) as exc:
            raise ToolError("INVALID_ARGUMENT", "Invalid message reference.") from exc

    async def message(self, ref: MessageRef) -> Message | None:
        self.backend_calls.append("message")
        self.reads.append(ref.key)
        if ref.chat_id in self.message_faults:
            raise self.message_faults[ref.chat_id]
        return next((m for m in self.stored_messages if m.message_ref == ref), None)

    async def messages(self, chat_id: str, message_ids: list[int]) -> dict[int, Message | None]:
        self.backend_calls.append("messages")
        self.batch_reads.append((chat_id, list(message_ids)))
        if chat_id in self.message_faults:
            raise self.message_faults[chat_id]
        found = {m.message_ref.message_id: m for m in self.stored_messages if m.chat.id == chat_id}
        return {mid: found.get(mid) for mid in message_ids}

    async def discussion_chat(self, chat: Chat) -> str:
        self.backend_calls.append("discussion_chat")
        return "-1002" if chat.type == "channel" else chat.id

    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef:
        self.backend_calls.append("thread_root")
        self.thread_calls += 1
        return MessageRef(chat_id=discussion_chat_id, message_id=10)

    async def fetch_topics(
        self,
        chat_id: str,
        *,
        offset: TopicOffset,
        limit: int = 30,
        query: str | None = None,
    ) -> TopicBatch:
        self.backend_calls.append("fetch_topics")
        self.topic_fetches.append((chat_id, offset, limit, query))
        if chat_id in self.topic_faults:
            raise self.topic_faults[chat_id]
        return self.topic_batches.get(
            (chat_id, offset),
            TopicBatch(
                topics=[],
                next_offset=None,
                exhausted=True,
                order_by_create_date=False,
                telegram_total_count=0,
            ),
        )

    async def get_topic(self, chat_id: str, topic_id: int) -> ForumTopic:
        self.backend_calls.append("get_topic")
        key = (chat_id, topic_id)
        self.topic_lookups.append(key)
        if key in self.topic_lookup_faults:
            raise self.topic_lookup_faults[key]
        if key not in self.topics:
            raise ToolError("TOPIC_UNAVAILABLE", "Topic was not found or is unavailable.")
        return self.topics[key]

    async def fetch(
        self,
        chat_id: str,
        *,
        offset: int = 0,
        limit: int = 30,
        query: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sender_id: str | None = None,
        content_type: ContentType | None = None,
        oldest_first: bool = False,
        thread_id: int | None = None,
        pinned_only: bool = False,
        topic_id: int | None = None,
    ) -> Batch:
        self.backend_calls.append("fetch")
        self.fetches.append(chat_id)
        self.fetch_offsets.append((chat_id, offset))
        self.fetch_pinned_flags.append(pinned_only)
        self.fetch_topic_ids.append(topic_id)
        if chat_id in self.faults:
            raise self.faults[chat_id]
        candidates = [
            m
            for m in self.stored_messages
            if m.chat.id == chat_id
            and (not pinned_only or m.message_ref.key in self.pinned_message_keys)
            and (topic_id is None or m.topic_id == topic_id)
            and (query is None or query in m.text)
            and (sender_id is None or m.sender.id == sender_id)
            and (
                thread_id is None or m.reply_to == MessageRef(chat_id=chat_id, message_id=thread_id)
            )
        ]
        candidates.sort(key=lambda m: m.message_ref.message_id, reverse=not oldest_first)
        if offset:
            candidates = [
                m
                for m in candidates
                if (
                    m.message_ref.message_id > offset
                    if oldest_first
                    else m.message_ref.message_id < offset
                )
            ]
        raw = candidates[:limit]
        filtered = [
            m
            for m in raw
            if (date_from is None or m.sent_at >= date_from)
            and (date_to is None or m.sent_at < date_to)
            and (content_type is None or m.content_type == content_type)
        ]
        return Batch(
            filtered, raw[-1].message_ref.message_id if raw else offset, len(candidates) <= limit
        )
