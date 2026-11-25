"""Preview and mutate one revision-bound message under locally configured rules."""

import asyncio
import re
import secrets
from dataclasses import dataclass
from weakref import WeakValueDictionary

from telegram_mcp.application.outbound_access import OutboundContext
from telegram_mcp.application.outbound_journal import OperationJournal
from telegram_mcp.application.outbound_ports import ActionSnapshot, Confirm
from telegram_mcp.application.ports import MessagePort
from telegram_mcp.domain.auth_notifications import authentication_notice
from telegram_mcp.domain.message_actions import (
    Action,
    ActionPreview,
    ActionReceipt,
    AvailableAction,
    Target,
)
from telegram_mcp.domain.models import MessageRef, ToolError
from telegram_mcp.domain.outbound import (
    OutboundOperation,
    SendReceipt,
    check_key,
    check_text,
    text_digest,
)
from telegram_mcp.domain.policy import SERVICE_NOTIFICATIONS_ID
from telegram_mcp.domain.references import is_canonical_chat_id


@dataclass(frozen=True)
class Selection:
    account: str
    ref: MessageRef
    source: str | None
    by_receipt: bool


class MessageActions:
    def __init__(self, context: OutboundContext, journal: OperationJournal, messages: MessagePort):
        self.context, self.journal, self.messages = context, journal, messages
        self._locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()

    async def _select(self, message_ref: str | None, operation_id: str | None) -> Selection:
        if (message_ref is None) == (operation_id is None):
            raise ToolError(
                "INVALID_ARGUMENT", "Supply exactly one message_ref or source_operation_id."
            )
        if not self.context.policies().message_actions.rules:
            raise ToolError("WRITES_DISABLED", "Configure local message action rules first.")
        account = await self.context.backend.account_id()
        if operation_id is not None:
            operation = await self.context.store.lookup_operation(account, operation_id)
            receipt = operation.receipt
            if not (
                operation.account_id == account
                and operation.kind in ("message", "file")
                and isinstance(receipt, SendReceipt)
                and receipt.status == "sent"
                and receipt.operation_id == operation_id
                and receipt.message_ref
                and operation.chat_id == receipt.chat_id == receipt.message_ref.chat_id
            ):
                raise ToolError(
                    "OPERATION_UNAVAILABLE",
                    "A successful send with an exact message reference is required.",
                )
            ref = receipt.message_ref
        else:
            assert message_ref is not None
            ref = await self.messages.resolve_message(message_ref)
        if not is_canonical_chat_id(ref.chat_id) or ref.chat_id == SERVICE_NOTIFICATIONS_ID:
            raise ToolError("ACCESS_DENIED", "Telegram service notifications are inaccessible.")
        source = operation_id or await self.context.store.sent_source(account, ref)
        return Selection(account, ref, source, operation_id is not None)

    async def _permissions(self, selection: Selection) -> tuple[ActionSnapshot, dict[Action, bool]]:
        ctx = self.context
        if await ctx.backend.account_id() != selection.account:
            raise ToolError("ACCOUNT_CHANGED", "Account changed during message inspection.")
        chat_id = selection.ref.chat_id
        policy = ctx.policies().message_actions
        read = ctx.access.policies.current()
        wanted = {f for r in policy.rules for f in r.folder_ids} | set(read.allowed_folder_ids)
        folders = (
            await ctx.backend.action_folders(chat_id, selection.account, wanted)
            if wanted
            else set()
        )
        readable = (
            read.allow_all_chats
            or chat_id in read.allowed_chat_ids
            or bool(folders.intersection(read.allowed_folder_ids))
        )
        receipt_only = not readable
        if receipt_only and not selection.by_receipt:
            raise ToolError("ACCESS_DENIED", "A direct reference requires ordinary read access.")
        if not any(
            rule.specificity(chat_id, folders) and (not receipt_only or "mcp_sent" in rule.targets)
            for rule in policy.rules
        ):
            raise ToolError("WRITE_ACCESS_DENIED", "No action rule covers this selection.")
        snapshot = await ctx.backend.action_target(selection.ref, selection.account)
        message = snapshot.message
        if (
            message.message_ref != selection.ref
            or message.chat.id != chat_id
            or not message.revision
        ):
            raise ToolError(
                "MESSAGE_UNAVAILABLE", "The exact message revision could not be established."
            )
        if message.omission_reason:
            raise ToolError(
                "AUTH_NOTIFICATION_FILTERED", "Authentication notices cannot be acted on."
            )
        targets: set[Target] = set() if receipt_only else {snapshot.target}
        if selection.source is not None and snapshot.target == "own" and message.outgoing is True:
            targets.add("mcp_sent")
        if selection.by_receipt and "mcp_sent" not in targets:
            raise ToolError(
                "MESSAGE_UNAVAILABLE",
                "The send receipt does not identify a current outgoing message.",
            )
        grants = policy.grants(chat_id, folders, targets, snapshot.supported)
        if receipt_only and not grants:
            raise ToolError("WRITE_ACCESS_DENIED", "No receipt-specific action is available.")
        if await ctx.backend.account_id() != selection.account:
            raise ToolError("ACCOUNT_CHANGED", "Account changed during message inspection.")
        return snapshot, grants

    async def preview(
        self, message_ref: str | None, source_operation_id: str | None
    ) -> ActionPreview:
        selection = await self._select(message_ref, source_operation_id)
        await self.context.check_cooldown(
            selection.account, f"message_action:{selection.ref.chat_id}"
        )
        try:
            snapshot, grants = await self._permissions(selection)
        except ToolError as exc:
            await self.journal.record_cooldown(selection.account, exc)
            raise
        msg = snapshot.message
        assert msg.revision
        return ActionPreview(
            message_ref=selection.ref,
            source_operation_id=selection.source,
            chat_title=msg.chat.title,
            chat_type=msg.chat.type,
            sender=msg.sender,
            text_preview=msg.text[:240],
            text_truncated=len(msg.text) > 240,
            revision=msg.revision,
            actions=[AvailableAction(action=a, confirmation_required=c) for a, c in grants.items()],
        )

    async def apply(
        self,
        action: Action,
        revision: str,
        key: str,
        message_ref: str | None,
        source_operation_id: str | None,
        text: str | None,
        confirm: Confirm | None,
    ) -> ActionReceipt:
        check_key(key)
        if not re.fullmatch(r"[a-f0-9]{64}", revision):
            raise ToolError(
                "INVALID_ARGUMENT", "Use the revision returned by preview_message_action."
            )
        if text is not None and authentication_notice(text):
            raise ToolError(
                "AUTH_NOTIFICATION_FILTERED",
                "Authentication notices cannot be submitted for editing.",
            )
        if text is not None:
            try:
                digest = text_digest(text)
            except UnicodeError:
                raise ToolError("INVALID_ARGUMENT", "Text contains invalid Unicode.") from None
        else:
            digest = None
        selected = await self._select(message_ref, source_operation_id)
        identity = {
            "ref": selected.ref.key,
            "source": source_operation_id,
            "action": action,
            "revision": revision,
            "text_sha256": digest,
        }
        kind = "edit" if action == "edit" else "delete"
        replay = await self.journal.replay(
            selected.account, selected.ref.chat_id, kind, key, identity
        )
        if replay is not None:
            assert isinstance(replay, ActionReceipt)
            return replay
        await self.context.check_cooldown(
            selected.account, f"message_action:{selected.ref.chat_id}"
        )
        receipt = ActionReceipt(
            operation_id=secrets.token_urlsafe(32),
            action=action,
            visibility=(
                "for_everyone"
                if action == "delete_for_everyone"
                else "for_me"
                if action == "delete_for_me"
                else None
            ),
            status="pending",
            message_ref=selected.ref,
            expected_revision=revision,
            source_operation_id=selected.source,
        )

        snapshot: ActionSnapshot

        async def validate() -> tuple[bool, str]:
            nonlocal snapshot
            snapshot, grants = await self._permissions(selected)
            if action not in grants:
                raise ToolError(
                    "ACTION_NOT_ALLOWED", "Telegram rights or local rules do not allow this action."
                )
            msg = snapshot.message
            if msg.revision != revision:
                raise ToolError(
                    "MESSAGE_CONFLICT", "The target revision changed. Preview the message again."
                )
            description = (
                f"{action} on message {selected.ref.key} in {msg.chat.title!r}. "
                f"Current excerpt: {msg.text[:240]!r}."
            )
            if text is not None:
                check_text(text, file=snapshot.caption)
                description += f" Replacement: {text!r}."
            return grants[action], description

        # Inspect before creating a journal entry, including notice filtering. Reuse
        # this inspection for the form, then fetch again under the target lock.
        try:
            initial: tuple[bool, str] | None = await validate()
        except ToolError as exc:
            await self.journal.record_cooldown(selected.account, exc)
            raise

        async def prepare(operation: OutboundOperation) -> tuple[bool, str]:
            nonlocal initial
            if initial is not None:
                result, initial = initial, None
                return result
            return await validate()

        async def dispatch(operation: OutboundOperation) -> ActionReceipt:
            await snapshot.dispatch(action, text)
            return receipt.model_copy(
                update={
                    "operation_id": operation.operation_id,
                    "status": "completed",
                    "rpc_accepted": True,
                }
            )

        lock = self._locks.setdefault((selected.account, selected.ref.key), asyncio.Lock())
        return await self.journal.run_action(
            selected.account, key, identity, receipt, prepare, confirm, dispatch, lock
        )
