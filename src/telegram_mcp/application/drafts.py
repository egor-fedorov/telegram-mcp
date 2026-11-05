"""Optimistic native draft updates without holding locks during user confirmation."""

import asyncio
from datetime import timedelta

from telegram_mcp.application.outbound_access import OutboundContext, require_confirmation
from telegram_mcp.application.outbound_ports import Confirm
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.outbound import Draft, DraftList, check_text


class DraftService:
    def __init__(self, context: OutboundContext):
        self.context = context
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, chat_ref: str) -> Draft:
        chat = await self.context.recipient(chat_ref, "allow_drafts")
        account = await self.context.backend.account_id()
        return await self._get(account, chat.id)

    async def _get(self, account: str, chat: str) -> Draft:
        current = await self.context.backend.draft(chat)
        owned = await self.context.store.owned_draft(account, chat)
        current.owned = owned is not None and owned.owned and current.revision == owned.revision
        if current.owned and owned is not None:
            current.file = owned.file
        return current

    async def list(self, limit: int = 30, cursor: str | None = None) -> DraftList:
        self.context.enabled("allow_drafts")
        if not 1 <= limit <= 100:
            raise ToolError("INVALID_ARGUMENT", "limit must be between 1 and 100.")
        ids = sorted(set(self.context.policies().allowed_write_chat_ids))
        if cursor is not None:
            if cursor not in ids:
                raise ToolError(
                    "INVALID_CURSOR", "The draft selection has changed; restart the listing."
                )
            ids = ids[ids.index(cursor) + 1 :]
        result = DraftList(drafts=[], next_cursor=ids[limit - 1] if len(ids) > limit else None)
        for chat in ids[:limit]:
            try:
                result.drafts.append(await self.get(chat))
            except ToolError as exc:
                result.issues.append(exc.issue(chat_id=chat))
        return result

    async def write(
        self,
        chat_ref: str,
        text: str,
        file_id: str | None,
        revision: str | None,
        confirm: Confirm | None,
    ) -> Draft:
        self.context.enabled("allow_drafts")
        if file_id is not None:
            raise ToolError(
                "DRAFT_ATTACHMENT_UNSUPPORTED",
                "Native file drafts are not supported. Keep the registered file_id for "
                "a separately confirmed send_file call, or create a text-only draft.",
            )
        if text or revision is None:
            check_text(text)
        chat = await self.context.recipient(chat_ref, "allow_drafts")
        account = await self.context.backend.account_id()
        current = await self._get(account, chat.id)
        await self.context.check_cooldown(account, f"draft:{chat.id}")
        self._check(current, revision)
        if revision is not None and current.exists and not current.owned:
            await require_confirmation(
                confirm,
                f"Replace your Telegram draft in {chat.title} ({chat.id})?\n"
                f"Account: {account}; current revision: {current.revision}\n"
                f"New text:\n{text}",
            )
        async with self._locks.setdefault(chat.id, asyncio.Lock()):
            await self.context.recipient(chat.id, "allow_drafts")
            if await self.context.backend.account_id() != account:
                raise ToolError("ACCOUNT_CHANGED", "Account changed while preparing the draft.")
            latest = await self._get(account, chat.id)
            if latest.revision != current.revision:
                raise ToolError(
                    "DRAFT_CONFLICT", "The draft changed; read it again before replacing it."
                )
            # Retain the previous snapshot before dispatch, but do not claim ownership yet.
            await self.context.store.save_draft(account, latest, latest)
            try:
                await self.context.check_cooldown(account, f"draft:{chat.id}")
                await self.context.backend.save_draft(chat.id, text)
            except ToolError as exc:
                if exc.code == "RATE_LIMITED" and exc.retry_after:
                    await self.context.store.set_cooldown(
                        account,
                        exc.scope or "account",
                        self.context.clock.utc_now() + timedelta(seconds=exc.retry_after),
                    )
                raise
            observed = await self.context.backend.draft(chat.id)
            if observed.text != text or observed.has_media:
                raise ToolError(
                    "DRAFT_CONFLICT",
                    "The observed draft differs from the requested draft. Read it again.",
                )
            observed.owned = True
            await self.context.store.save_draft(account, observed, latest)
            return observed

    @staticmethod
    def _check(current: Draft, revision: str | None) -> None:
        if not current.supported:
            raise ToolError("DRAFT_UNSUPPORTED", "This native draft uses unsupported rich content.")
        if revision is None and current.exists:
            raise ToolError(
                "DRAFT_EXISTS", "A draft already exists; use update_draft with its revision."
            )
        if revision is not None and revision != current.revision:
            raise ToolError(
                "DRAFT_CONFLICT", "The draft changed; read it again before replacing it."
            )
