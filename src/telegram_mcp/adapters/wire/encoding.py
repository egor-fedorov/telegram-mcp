"""Project domain results into compact MCP payloads with identical text content."""

import json
from typing import Any

from mcp_types import CallToolResult, ResourceLink, TextContent
from pydantic_core import to_json

from telegram_mcp.domain import changes, models, watches

# These are protocol controls, not unknown optional metadata. Their null values
# distinguish exhaustion, unavailable anchors and unverified authorization.
_NULL_CONTROLS = {
    "authorized",
    "next_cursor",
    "next_checkpoint",
    "next_text_offset",
    "anchor",
    "before_exhausted",
    "after_exhausted",
    "batch_id",
}
_EMPTY_METADATA = {"entities", "attachments"}


def _project(value: Any, chats: dict[str, Any]) -> Any:
    if isinstance(value, models.MessageRef):
        return value.key
    if isinstance(value, models.Scope):
        return _project_scope(value)
    if isinstance(value, models.Model):
        return _project_model(value, chats)
    if isinstance(value, dict):
        return {
            key: _project(item, chats)
            for key, item in value.items()
            if item is not None or key in _NULL_CONTROLS
        }
    if isinstance(value, list):
        return [_project(item, chats) for item in value]
    return value


def _project_scope(scope: models.Scope) -> dict[str, Any]:
    return {
        "folder_ids": scope.folder_ids,
        "chat_ids": scope.chat_ids[:20],
        "chat_count": len(scope.chat_ids),
        "chat_ids_truncated": len(scope.chat_ids) > 20,
        "resolved_at": scope.resolved_at,
        "pending_chat_ids": scope.pending_chat_ids[:20],
        "pending_chat_count": len(scope.pending_chat_ids),
        "pending_chat_ids_truncated": len(scope.pending_chat_ids) > 20,
        "complete": scope.complete,
    }


def _project_model(value: models.Model, chats: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, models.Message):
        _remember_chat(value.chat, chats)
    data: dict[str, Any] = {}
    for name in type(value).model_fields:
        item = getattr(value, name)
        if not _omitted(value, name, item):
            data[name] = _project(item, chats)
    return data


def _remember_chat(chat: models.Chat, chats: dict[str, Any]) -> None:
    # One response snapshot per canonical chat ID; message references carry
    # the key. Forwarded sources are not fetched or added to this map.
    summary = {"title": chat.title, "type": chat.type}
    if chat.username is not None:
        summary["username"] = chat.username
    chats[chat.id] = summary


def _omitted(owner: models.Model, name: str, item: Any) -> bool:
    """Absent fields: a message's chat, unknown metadata and empty optional lists/senders."""
    return (
        (isinstance(owner, models.Message) and name == "chat")
        or (item is None and name not in _NULL_CONTROLS)
        or (name in _EMPTY_METADATA and item == [])
        or (isinstance(item, models.Sender) and item == models.Sender())
    )


def encode(value: models.Model | dict[str, object] | list[models.Folder]) -> CallToolResult:
    """Return identical compact JSON in structured and text MCP content.

    Validation uses the public SDK's Annotated[CallToolResult, DTO] contract; it
    never reserializes the payload or fills in omitted optional fields.
    """
    chats: dict[str, Any] = {}
    payload = _project(value, chats)
    if isinstance(
        value,
        (
            models.Page,
            models.MessageResult,
            models.AlbumResult,
            models.ContextResult,
            changes.ChangesResult,
            watches.WatchChangesResult,
        ),
    ):
        payload = {"chats": chats, **payload}
    elif isinstance(value, list):
        payload = {"folders": payload}
    # Domain values can contain datetime objects. Pydantic's public JSON encoder
    # preserves the established UTC spelling and Unicode without a second model.
    text = to_json(payload).decode("utf-8")
    result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=json.loads(text),
    )
    if isinstance(value, models.DownloadResult):
        result.content.append(
            ResourceLink(
                type="resource_link",
                uri=value.resource_uri,
                name=value.name,
                mime_type=value.mime_type,
                size=value.size,
                description="Temporary attachment; read before expires_at from the tool result.",
            )
        )
    if (
        isinstance(value, (changes.ChangesResult, changes.ExportHistoryResult))
        and value.export is not None
    ):
        exported = value.export
        result.content.append(
            ResourceLink(
                type="resource_link",
                uri=exported.resource_uri,
                name=exported.name,
                mime_type=exported.mime_type,
                size=exported.size,
                description="Temporary source-linked export; read before expires_at.",
            )
        )
    return result
