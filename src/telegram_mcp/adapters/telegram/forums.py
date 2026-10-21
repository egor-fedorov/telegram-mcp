"""Forum topic metadata and native topic pagination."""

from datetime import datetime

from telethon import functions, types

from telegram_mcp.adapters.telegram.chats import ChatGateway
from telegram_mcp.adapters.telegram.mapping import forum_topic_from_tl, peer_id, utc, valid_topic_id
from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.ports import TopicBatch, TopicOffset
from telegram_mcp.domain.models import ForumTopic, Issue, ToolError


class ForumGateway:
    def __init__(self, runtime: TelegramRuntime, chats: ChatGateway):
        self.runtime = runtime
        self.chats = chats

    async def fetch_topics(
        self,
        chat_id: str,
        *,
        offset: TopicOffset,
        limit: int = 30,
        query: str | None = None,
    ) -> TopicBatch:
        chat = await self.chats.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        response = await self.runtime.call(
            client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    q=query,
                    offset_date=offset.date,
                    offset_id=offset.message_id,
                    offset_topic=offset.topic_id,
                    limit=limit,
                )
            )
        )
        topics: list[ForumTopic] = []
        issues: list[Issue] = []
        for raw in response.topics:
            if (
                not isinstance(raw, (types.ForumTopic, types.ForumTopicDeleted))
                or raw.id <= 0
                or isinstance(raw, types.ForumTopic)
                and not raw.short
                and peer_id(raw.peer) != chat_id
            ):
                issues.append(
                    ToolError(
                        "PAGINATION_INCOMPLETE",
                        "Telegram returned an unrelated topic or invalid topic data.",
                    ).issue(chat_id)
                )
                continue
            topics.append(forum_topic_from_tl(raw, chat))
        order_by_create_date = bool(response.order_by_create_date)
        if not response.topics:
            return TopicBatch(topics, None, True, order_by_create_date, response.count, issues)
        tail = response.topics[-1]
        date = None
        if (
            isinstance(tail, types.ForumTopic)
            and tail.id > 0
            and not tail.short
            and peer_id(tail.peer) == chat_id
        ):
            if order_by_create_date:
                date = tail.date
            else:
                date = next(
                    (
                        message.date
                        for message in response.messages
                        if not isinstance(message, types.MessageEmpty)
                        and getattr(message, "id", None) == tail.top_message
                        and getattr(message, "peer_id", None) is not None
                        and peer_id(message.peer_id) == chat_id
                        and isinstance(getattr(message, "date", None), datetime)
                    ),
                    None,
                )
        if date is None:
            issues.append(
                ToolError(
                    "PAGINATION_INCOMPLETE",
                    "The final topic has insufficient metadata to continue safely.",
                ).issue(chat_id)
            )
            return TopicBatch(topics, None, True, order_by_create_date, response.count, issues)
        next_offset = TopicOffset(utc(date), max(0, tail.top_message), tail.id)
        return TopicBatch(topics, next_offset, False, order_by_create_date, response.count, issues)

    async def get_topic(self, chat_id: str, topic_id: int) -> ForumTopic:
        if not valid_topic_id(topic_id):
            raise ToolError("INVALID_ARGUMENT", "topic_id must be a positive 32-bit integer.")
        chat = await self.chats.resolve_chat(chat_id)
        if not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum.")
        client = await self.runtime.ensure()
        entity = await self.runtime.call(client.get_input_entity(int(chat_id)))
        try:
            response = await self.runtime.call(
                client(functions.messages.GetForumTopicsByIDRequest(peer=entity, topics=[topic_id]))
            )
        except ToolError as exc:
            if exc.code == "TELEGRAM_ERROR" and exc.detail == "MsgIdInvalidError":
                raise ToolError(
                    "TOPIC_UNAVAILABLE", "The requested forum topic is unavailable."
                ) from None
            raise
        matches = [
            raw for raw in getattr(response, "topics", []) if getattr(raw, "id", None) == topic_id
        ]
        if len(matches) == 1:
            raw = matches[0]
            if isinstance(raw, types.ForumTopic) and (raw.short or peer_id(raw.peer) == chat_id):
                return forum_topic_from_tl(raw, chat)
        raise ToolError("TOPIC_UNAVAILABLE", "The requested forum topic is unavailable.")
