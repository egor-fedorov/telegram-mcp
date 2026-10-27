"""Authorization, connection and policy status."""

from typing import Annotated, Literal, NotRequired

from pydantic import Field, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import CONFIG, ChatID, Issue


@with_config(CONFIG)
class Status(TypedDict):
    authorized: bool | None
    connected: bool
    error: NotRequired[Issue]
    mode: Literal["read_only"]
    allow_all_chats: bool
    allow_public_channel_reads: bool
    allow_public_channel_discovery: bool
    allow_attachment_downloads: bool
    archive_enabled: bool
    attachment_max_bytes: Annotated[int, Field(gt=0)]
    download_ttl_seconds: Annotated[int, Field(gt=0)]
    download_max_bytes: Annotated[int, Field(gt=0)]
    download_max_files: Annotated[int, Field(gt=0)]
    allowed_chat_ids: list[ChatID]
    allowed_folder_ids: list[int]
    cursor_ttl_seconds: Annotated[int, Field(gt=0)]
