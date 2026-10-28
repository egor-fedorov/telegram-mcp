"""Fresh request-local access decisions for explicit, linked and forum sources."""

from dataclasses import dataclass, field

from telegram_mcp.application.ports import ChatPort, PolicyProvider
from telegram_mcp.domain.folders import allowed_ids
from telegram_mcp.domain.models import Chat, Folder, ToolError
from telegram_mcp.domain.policy import SERVICE_NOTIFICATIONS_ID, ReadPolicy
from telegram_mcp.domain.references import is_canonical_chat_id


@dataclass
class AccessContext:
    chats: list[Chat]
    folders: list[Folder]
    allowed: set[str]
    policy: ReadPolicy
    chat_port: ChatPort
    public: dict[str, Chat] = field(default_factory=dict)

    def _require_readable_id(self, chat_id: str) -> None:
        # int() accepts Unicode digits; raw aliases must not bypass string-based policy checks.
        if not is_canonical_chat_id(chat_id):
            raise ToolError(
                "INVALID_REFERENCE",
                "Use a numeric chat ID with ASCII digits, no leading zeros, "
                "and an optional minus sign.",
            )
        if chat_id == SERVICE_NOTIFICATIONS_ID:
            raise ToolError(
                "ACCESS_DENIED",
                "Telegram service notifications contain login codes and are never readable.",
            )

    def require_access(self, chat_id: str, *, same_source: str | None = None) -> None:
        """Linked peers require normal access; parents may share their anchor's grant."""
        self._require_readable_id(chat_id)
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
        self._require_readable_id(chat_id)
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

    async def load(self, *, explicit: bool = False) -> AccessContext:
        policy = self.policies.current()
        if explicit and not policy.allowed_folder_ids:
            await self.chats.check_authorization()
            policy = self.policies.current()
            if not policy.allowed_folder_ids:
                return AccessContext([], [], set(policy.allowed_chat_ids), policy, self.chats)
        chats, folders = await self.chats.catalog()
        policy = self.policies.current()
        return AccessContext(
            list(chats), folders, allowed_ids(policy, chats, folders), policy, self.chats
        )
