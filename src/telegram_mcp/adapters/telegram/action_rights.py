"""Conservative Telegram capabilities for one freshly fetched ordinary message."""

from datetime import datetime
from typing import Any

from telethon import types

from telegram_mcp.adapters.telegram.mapping import peer_id, utc
from telegram_mcp.domain.message_actions import Action, Target
from telegram_mcp.domain.models import Chat, ToolError


def check_ordinary(entity: Any, raw: Any, chat: Chat) -> None:
    if (
        not isinstance(entity, (types.User, types.Chat, types.Channel))
        or any(
            getattr(entity, key, False)
            for key in ("min", "left", "deactivated", "deleted", "migrated_to", "monoforum")
        )
        or getattr(getattr(entity, "banned_rights", None), "view_messages", False)
    ):
        raise ToolError("ACTION_UNSUPPORTED", "This dialog is unavailable for message actions.")
    if (
        raw.action
        or raw.from_scheduled
        or raw.schedule_repeat_period
        or raw.quick_reply_shortcut_id
        or raw.paid_message_stars
        or raw.suggested_post
        or raw.paid_suggested_post_stars
        or raw.paid_suggested_post_ton
        or isinstance(raw.media, types.MessageMediaPaidMedia)
        or (chat.is_forum and raw.id == 1)
    ):
        raise ToolError(
            "ACTION_UNSUPPORTED", "Service, topic-root, scheduled and paid messages are excluded."
        )


def target_class(raw: Any, chat: Chat, account: str) -> Target:
    author = raw.from_id
    if chat.type == "saved" and chat.id == account:
        return "own"
    if raw.out and (
        author is None
        or peer_id(author) == account
        or (chat.type == "channel" and peer_id(author) == chat.id)
    ):
        return "own"
    if raw.out:
        raise ToolError("ACTION_UNSUPPORTED", "An outgoing message has an ambiguous sender.")
    if chat.type in ("channel", "group"):
        return "group_others"
    if chat.type in ("private", "bot"):
        return "private_incoming"
    raise ToolError("ACTION_UNSUPPORTED", "The author of the message could not be established.")


def simple_edit(raw: Any) -> bool:
    if any(
        (
            raw.fwd_from,
            raw.entities,
            raw.reply_markup,
            raw.via_bot_id,
            raw.via_business_bot_id,
            raw.rich_message,
            raw.invert_media,
            raw.grouped_id,
            raw.guestchat_via_from,
        )
    ):
        return False
    if raw.media is None or isinstance(raw.media, types.MessageMediaEmpty):
        return True
    if isinstance(raw.media, types.MessageMediaPhoto):
        return isinstance(raw.media.photo, types.Photo) and not (
            raw.media.ttl_seconds or raw.media.live_photo
        )
    if isinstance(raw.media, types.MessageMediaDocument):
        document = raw.media.document
        return (
            isinstance(document, types.Document)
            and not raw.media.ttl_seconds
            and not any(
                isinstance(
                    attr, (types.DocumentAttributeSticker, types.DocumentAttributeCustomEmoji)
                )
                for attr in document.attributes
            )
        )
    return False


def native_actions(
    entity: Any, raw: Any, chat: Chat, target: Target, config: Any, now: datetime
) -> set[Action]:
    owner = target == "own"
    admin = getattr(entity, "admin_rights", None)
    creator = bool(getattr(entity, "creator", False))
    delete_right = creator or bool(getattr(admin, "delete_messages", False))
    age = max(0.0, (now - utc(raw.date)).total_seconds())
    result: set[Action] = set()
    if isinstance(entity, types.Channel):
        if delete_right or (owner and (entity.megagroup or getattr(admin, "post_messages", False))):
            result.add("delete_for_everyone")
    else:
        result.add("delete_for_me")
        if (
            chat.type == "private"
            and (owner or config.revoke_pm_inbox)
            and age <= config.revoke_pm_time_limit
        ):
            result.add("delete_for_everyone")
        if chat.type == "group" and (owner or delete_right) and age <= config.revoke_time_limit:
            result.add("delete_for_everyone")
    edit_right = creator or bool(getattr(admin, "edit_messages", False))
    if chat.type == "channel":
        edit_allowed = edit_right or bool(getattr(admin, "post_messages", False))
    else:
        edit_allowed = (
            chat.type == "saved"
            or age <= config.edit_time_limit
            or (chat.is_supergroup and (creator or bool(getattr(admin, "pin_messages", False))))
        )
    if owner and edit_allowed and simple_edit(raw):
        result.add("edit")
    return result
