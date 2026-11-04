"""Shared account and recipient checks for explicitly enabled outbound features."""

import math
from collections.abc import Callable
from dataclasses import dataclass

from telegram_mcp.application.access import AccessFactory
from telegram_mcp.application.outbound_ports import Confirm, OutboundPort, OutboundStorePort
from telegram_mcp.application.ports import ChatPort, Clock
from telegram_mcp.domain.models import Chat, ToolError
from telegram_mcp.domain.outbound import WritePolicy


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

    async def recipient(self, ref: str, capability: str, *, bot: bool = False) -> Chat:
        self.enabled(capability)
        chat = await self.chats.resolve_chat(ref, refresh=True)
        context = await self.access.load(explicit=True)
        context.require_access(chat.id)
        self.enabled(capability)
        if chat.id not in self.policies().allowed_write_chat_ids:
            raise ToolError("WRITE_ACCESS_DENIED", "Recipient is outside allowed_write_chat_ids.")
        if bot and chat.type != "bot":
            raise ToolError(
                "BUTTON_UNSUPPORTED", "Callbacks require an explicitly allowed private bot dialog."
            )
        if chat.is_forum:
            raise ToolError("WRITE_UNSUPPORTED", "Forum topic writes and drafts are not supported.")
        return chat
