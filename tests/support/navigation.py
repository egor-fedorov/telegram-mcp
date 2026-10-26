from telegram_mcp.application.ports import TopicBatch, TopicOffset
from telegram_mcp.domain.models import Chat, ForumTopic
from tests.support.fakes import BASE, FakeBackend, msg


def navigation_backend() -> FakeBackend:
    forum = Chat(
        id="-1001",
        title="Project forum",
        username="project_forum",
        type="group",
        is_supergroup=True,
        is_forum=True,
    )
    root = msg(forum.id, 10, "Topic root")
    reply = msg(forum.id, 12, "Topic reply", reply_to=root.message_ref)
    pin = msg(forum.id, 20, "Chat-wide pinned message")
    backend = FakeBackend([root, reply, pin], chats=[forum])
    backend.pinned_message_keys = {root.message_ref.key, pin.message_ref.key}
    backend.topic_batches[(forum.id, TopicOffset())] = TopicBatch(
        topics=[
            ForumTopic(
                id=10,
                chat_id=forum.id,
                title="Roadmap",
                created_at=BASE,
                is_closed=False,
                is_hidden=False,
                is_pinned=True,
                unread_count=2,
                root_message_ref=root.message_ref,
                top_message_ref=reply.message_ref,
                url="https://t.me/project_forum/10",
            ),
            ForumTopic(
                id=1,
                chat_id=forum.id,
                title="General",
                is_general=True,
                top_message_ref=pin.message_ref,
            ),
            ForumTopic(id=30, chat_id=forum.id, is_short=True),
            ForumTopic(id=40, chat_id=forum.id, is_deleted=True),
        ],
        next_offset=None,
        exhausted=True,
        order_by_create_date=False,
        telegram_total_count=4,
    )
    return backend
