"""Local authorization rules and public contracts for single-message actions."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from telegram_mcp.domain.models import ChatType, Issue, MessageRef, Model, Sender
from telegram_mcp.domain.policy import SERVICE_NOTIFICATIONS_ID
from telegram_mcp.domain.references import is_canonical_chat_id

Action = Literal["edit", "delete_for_me", "delete_for_everyone"]
Visibility = Literal["for_me", "for_everyone"]
Target = Literal["mcp_sent", "own", "group_others", "private_incoming"]
ConfirmationPolicy = Literal["per_rule", "always", "never"]


class ActionRule(Model):
    chat_ids: tuple[str, ...] = ()
    folder_ids: tuple[Annotated[int, Field(strict=True, gt=0)], ...] = ()
    all_chats: StrictBool = False
    targets: tuple[Target, ...] = Field(min_length=1)
    actions: tuple[Action, ...] = Field(min_length=1)
    require_confirmation: StrictBool = True

    @model_validator(mode="after")
    def check_selector(self) -> Self:
        if sum(map(bool, (self.chat_ids, self.folder_ids, self.all_chats))) != 1:
            raise ValueError("choose exactly one of chat_ids, folder_ids or all_chats = true")
        if any(
            not is_canonical_chat_id(cid) or cid == SERVICE_NOTIFICATIONS_ID
            for cid in self.chat_ids
        ):
            raise ValueError("use canonical chat IDs outside Telegram service notifications")
        return self

    def specificity(self, chat_id: str, folders: set[int]) -> int:
        if chat_id in self.chat_ids:
            return 3
        if folders.intersection(self.folder_ids):
            return 2
        return int(self.all_chats)


class ActionPolicy(Model):
    confirmation: ConfirmationPolicy = "per_rule"
    rules: tuple[ActionRule, ...] = ()

    def grants(
        self, chat_id: str, folders: set[int], targets: set[Target], supported: set[Action]
    ) -> dict[Action, bool]:
        grants = {}
        for action in sorted(supported):
            matches = [
                (level, rule.require_confirmation)
                for rule in self.rules
                if action in rule.actions
                and targets.intersection(rule.targets)
                and (level := rule.specificity(chat_id, folders))
            ]
            if matches:
                grants[action] = self.confirmation == "always" or (
                    self.confirmation == "per_rule" and max(matches)[1]
                )
        return grants


class AvailableAction(Model):
    action: Action
    confirmation_required: bool


class ActionPreview(Model):
    message_ref: MessageRef
    source_operation_id: str | None = None
    chat_title: str
    chat_type: ChatType
    sender: Sender
    text_preview: str
    text_truncated: bool
    revision: str
    actions: list[AvailableAction]


class ActionReceipt(Model):
    operation_id: str
    action: Action
    visibility: Visibility | None = None
    message_ref: MessageRef
    source_operation_id: str | None = None
    expected_revision: str
    status: Literal["pending", "completed", "failed", "unknown"]
    rpc_accepted: bool = False
    error: Issue | None = None
    retry_at: AwareDatetime | None = None
