"""Evaluate Telegram dialog filters, then apply the server's access policy."""

from telegram_mcp.config import Settings
from telegram_mcp.models import Chat, Folder, ToolError


def contains(folder: Folder, chat: Chat) -> bool:
    if chat.id in folder.excluded_chat_ids:
        return False
    if chat.id in folder.pinned_chat_ids or chat.id in folder.included_chat_ids:
        return True
    if chat.is_community:
        return False
    if folder.exclude_archived and chat.is_archived:
        return False
    if (
        folder.exclude_muted
        and chat.is_muted
        and not (chat.unread_mentions_count and not chat.is_archived)
    ):
        return False
    if folder.exclude_read and not (
        chat.unread_count or chat.marked_unread or chat.unread_mentions_count
    ):
        return False
    if chat.type == "bot":
        return folder.include_bots
    if chat.type == "group":
        return folder.include_groups
    if chat.type == "channel":
        return folder.include_channels
    return folder.include_contacts if chat.is_contact else folder.include_non_contacts


def allowed_ids(settings: Settings, chats: list[Chat], folders: list[Folder]) -> set[str]:
    if settings.allow_all_chats:
        return {c.id for c in chats}
    chosen = [f for f in folders if f.id in settings.allowed_folder_ids]
    return set(settings.allowed_chat_ids) | {
        c.id for c in chats if any(contains(f, c) for f in chosen)
    }


def select_chats(
    chats: list[Chat],
    folders: list[Folder],
    allowed: set[str],
    folder_ids: list[int] | None = None,
    chat_ids: list[str] | None = None,
    query: str | None = None,
    chat_type: str | None = None,
) -> list[Chat]:
    if folder_ids == [] or chat_ids == []:
        raise ToolError(
            "INVALID_ARGUMENT", "Use null for an unrestricted selector, not an empty list."
        )
    requested = set(folder_ids or [])
    if missing := requested - {f.id for f in folders}:
        raise ToolError("FOLDER_NOT_FOUND", f"Unknown folder IDs: {sorted(missing)}")
    if chat_ids and set(chat_ids) - allowed:
        raise ToolError(
            "ACCESS_DENIED", "One or more requested chats are outside the access policy."
        )
    results: list[Chat] = []
    seen: set[str] = set()
    for chat in chats:
        if chat.id not in allowed or chat.id in seen:
            continue
        matched = [
            f.id for f in folders if contains(f, chat) and (not requested or f.id in requested)
        ]
        if requested and not matched:
            continue
        if chat_ids is not None and chat.id not in chat_ids:
            continue
        if query and query.casefold() not in f"{chat.title} {chat.username or ''}".casefold():
            continue
        if chat_type and chat.type != chat_type:
            continue
        seen.add(chat.id)
        results.append(
            chat.model_copy(
                update={
                    "matched_folder_ids": matched,
                    "pinned_in_folder_ids": [f.id for f in folders if chat.id in f.pinned_chat_ids],
                }
            )
        )
    if len(requested) == 1:
        folder = next(f for f in folders if f.id in requested)
        ranks = {cid: i for i, cid in enumerate(folder.pinned_chat_ids)}
        results.sort(key=lambda c: ranks.get(c.id, len(ranks)))
    return results
