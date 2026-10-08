from datetime import UTC, datetime, timedelta

from telegram_mcp.backend import Batch
from telegram_mcp.models import Chat, ContentType, Folder, Message, MessageRef, ToolError

BASE = datetime(2026, 9, 1, tzinfo=UTC)


def msg(
    cid: str,
    mid: int,
    text: str = "budget discussion",
    *,
    minute: int | None = None,
    content: ContentType = "text",
    reply_to: MessageRef | None = None,
) -> Message:
    return Message(
        message_ref=MessageRef(chat_id=cid, message_id=mid),
        chat=Chat(id=cid, title=f"Chat {cid}", type="group"),
        sent_at=BASE + timedelta(minutes=mid if minute is None else minute),
        text=text,
        content_type=content,
        reply_to=reply_to,
    )


class FakeBackend:
    def __init__(
        self,
        messages: list[Message] | None = None,
        chats: list[Chat] | None = None,
        folders: list[Folder] | None = None,
    ):
        self.messages = messages or []
        self.chats = (
            chats
            if chats is not None
            else list({m.chat.id: m.chat for m in self.messages}.values())
        )
        self.folders = folders or []
        self.reads: list[str] = []
        self.fetches: list[str] = []
        self.faults: dict[str, ToolError] = {}
        self.closed = False
        self.thread_calls = 0

    async def status(self) -> dict[str, object]:
        return {"authorized": True, "connected": True}

    async def close(self) -> None:
        self.closed = True

    async def catalog(self) -> tuple[list[Chat], list[Folder]]:
        return self.chats, self.folders

    async def resolve_chat(self, ref: str) -> Chat:
        for chat in self.chats:
            if ref in (chat.id, chat.username):
                return chat
        raise ToolError("NOT_FOUND", "Unknown chat")

    async def chat_details(self, chat: Chat) -> dict[str, object]:
        return {"description": "Example", "server_can_send": False}

    async def resolve_message(self, ref: str | MessageRef) -> MessageRef:
        if isinstance(ref, MessageRef):
            return ref
        cid, mid = ref.split(":")
        return MessageRef(chat_id=cid, message_id=int(mid))

    async def message(self, ref: MessageRef) -> Message | None:
        self.reads.append(ref.key)
        return next((m for m in self.messages if m.message_ref == ref), None)

    async def discussion_chat(self, chat: Chat) -> str:
        return "-1002" if chat.type == "channel" else chat.id

    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef:
        self.thread_calls += 1
        return MessageRef(chat_id=discussion_chat_id, message_id=10)

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
    ) -> Batch:
        self.fetches.append(chat_id)
        if chat_id in self.faults:
            raise self.faults[chat_id]
        candidates = [
            m
            for m in self.messages
            if m.chat.id == chat_id
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
