"""Fresh request-local access decisions for explicit, linked and forum sources."""

from dataclasses import dataclass, field

from telegram_mcp.application.ports import ChatPort, PolicyProvider
from telegram_mcp.domain.folders import allowed_ids
from telegram_mcp.domain.models import Chat, Folder, ToolError
from telegram_mcp.domain.policy import ReadPolicy


@dataclass
class AccessContext:
    chats: list[Chat]
    folders: list[Folder]
    allowed: set[str]
    policy: ReadPolicy
    chat_port: ChatPort
    public: dict[str, Chat] = field(default_factory=dict)

    def require_access(self, chat_id: str, *, same_source: str | None = None) -> None:
        """Linked peers require normal access; parents may share their anchor's grant."""
        if (
            not self.policy.allow_all_chats
            and chat_id not in self.allowed
            and chat_id != same_source
        ):
            raise ToolError(
                "ACCESS_DENIED", "Chat is outside the server's configured access policy."
            )

    async def allow_explicit(self, chat_id: str) -> None:
        """Authorize a user-selected source without extending the dialog catalog."""
        if self.policy.allow_all_chats or chat_id in self.allowed or chat_id in self.public:
            return
        if not self.policy.allow_public_channel_reads:
            self.require_access(chat_id)
        self.public[chat_id] = await self.chat_port.public_channel(chat_id)

    def require_selected(self, chat_id: str) -> None:
        if chat_id not in self.public:
            self.require_access(chat_id)

    def require_forum(self, chat: Chat) -> None:
        self.require_access(chat.id)
        if chat.type != "group" or not chat.is_supergroup or not chat.is_forum:
            raise ToolError("FORUM_UNAVAILABLE", "This chat is not a supported forum supergroup.")


class AccessFactory:
    def __init__(self, chats: ChatPort, policies: PolicyProvider):
        self.chats = chats
        self.policies = policies

    async def load(self) -> AccessContext:
        chats, folders = await self.chats.catalog()
        policy = self.policies.current()
        return AccessContext(
            list(chats), folders, allowed_ids(policy, chats, folders), policy, self.chats
        )
