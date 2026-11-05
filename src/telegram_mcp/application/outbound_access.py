"""Shared account and recipient checks for explicitly enabled outbound features."""

import math
from collections.abc import Callable
from dataclasses import dataclass

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.outbound_ports import Confirm, OutboundPort, OutboundStorePort
from telegram_mcp.application.ports import ChatPort, Clock
from telegram_mcp.domain.models import Chat, ToolError
from telegram_mcp.domain.outbound import RecipientBinding, WriteGrant, WritePolicy
from telegram_mcp.domain.policy import SERVICE_NOTIFICATIONS_ID
from telegram_mcp.domain.references import chat_target, is_canonical_chat_id


def recipient_selector(ref: str) -> str:
    return str(chat_target(ref)).removeprefix("@").casefold()


async def require_confirmation(confirm: Confirm | None, description: str) -> None:
    if confirm is None:
        raise ToolError(
            "CONFIRMATION_UNSUPPORTED",
            "This action requires MCP form elicitation in the connected client.",
        )
    if not await confirm(description):
        raise ToolError(
            "CONFIRMATION_DECLINED", "The action was not confirmed; nothing was dispatched."
        )


@dataclass
class OutboundContext:
    backend: OutboundPort
    store: OutboundStorePort
    access: AccessFactory
    chats: ChatPort
    policies: Callable[[], WritePolicy]
    clock: Clock

    async def check_cooldown(self, account: str, scope: str) -> None:
        now = self.clock.utc_now()
        waits = await self.store.cooldowns(account, now)
        seconds = max(
            (
                (until - now).total_seconds()
                for key, until in waits.items()
                if key in ("account", scope)
            ),
            default=0,
        )
        if seconds > 0:
            raise ToolError(
                "RATE_LIMITED",
                "Telegram requires a wait before this action.",
                math.ceil(seconds),
                scope="account" if "account" in waits else scope,
            )

    def enabled(self, capability: str) -> None:
        if not getattr(self.policies(), capability):
            raise ToolError("WRITES_DISABLED", f"Enable {capability} in local configuration.")

    async def recipient(
        self, ref: str, capability: str, *, bot: bool = False, grant: WriteGrant | None = None
    ) -> Chat:
        self.enabled(capability)
        chat = await self.chats.resolve_chat(ref, refresh=True)
        await self.authorize(chat, capability, bot=bot, grant=grant)
        return chat

    async def bind(self, ref: str, capability: str, *, bot: bool = False) -> RecipientBinding:
        self.enabled(capability)
        chat = await self.chats.resolve_chat(ref, refresh=True)
        grant = await self.authorize(chat, capability, bot=bot)
        return RecipientBinding(
            chat_id=chat.id,
            title=chat.title,
            type=chat.type,
            username=chat.username,
            selector=recipient_selector(ref),
            grant=grant,
        )

    async def revalidate(self, binding: RecipientBinding, capability: str) -> None:
        self.enabled(capability)
        chat = await self.chats.resolve_chat(binding.chat_id, refresh=True)
        if chat.id != binding.chat_id or chat.type != binding.type:
            raise ToolError("WRITE_ACCESS_DENIED", "The selected recipient identity has changed.")
        await self.authorize(
            chat, capability, grant=binding.grant, bot=capability == "allow_bot_callbacks"
        )

    async def require_read(self, chat_id: str) -> None:
        context = await self.access.load(explicit=True)
        context.require_access(chat_id)

    async def authorize(
        self, chat: Chat, capability: str, *, grant: WriteGrant | None = None, bot: bool = False
    ) -> WriteGrant:
        self.enabled(capability)
        if not is_canonical_chat_id(chat.id) or chat.id == SERVICE_NOTIFICATIONS_ID:
            raise ToolError("ACCESS_DENIED", "Telegram service notifications are inaccessible.")
        if bot and chat.type != "bot":
            raise ToolError(
                "BUTTON_UNSUPPORTED", "Callbacks require a permitted private bot dialog."
            )
        if chat.is_forum:
            raise ToolError("WRITE_UNSUPPORTED", "Forum topic writes and drafts are not supported.")
        policy = self.policies()
        sending = capability in ("allow_send_messages", "allow_send_files")
        if grant is None:
            if policy.allow_all_write_chats:
                grant = "all_chats"
            elif chat.id in policy.allowed_write_chat_ids:
                grant = "allowlist"
            elif sending and policy.allow_one_off_sends:
                grant = "one_off"
            else:
                grant = "allowlist"
        if grant == "allowlist" or not sending:
            await self.require_read(chat.id)
        self.enabled(capability)
        policy = self.policies()
        permitted = {
            "all_chats": policy.allow_all_write_chats,
            "allowlist": chat.id in policy.allowed_write_chat_ids,
            "one_off": sending and policy.allow_one_off_sends and chat.type in ("private", "bot"),
        }[grant]
        if not permitted:
            raise ToolError(
                "WRITE_ACCESS_DENIED",
                "The local configuration does not permit this recipient grant.",
            )
        return grant
