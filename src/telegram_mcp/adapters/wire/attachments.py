"""Selected attachment downloads."""

from typing import NotRequired

from pydantic import AwareDatetime, with_config
from typing_extensions import TypedDict

from telegram_mcp.adapters.wire.common import CONFIG, Count, MessageKey, SavedFile


@with_config(CONFIG)
class DownloadResult(TypedDict):
    attachment_id: str
    message_ref: MessageKey
    name: str
    mime_type: str
    size: Count
    sha256: str
    local_path: str
    resource_uri: str
    expires_at: AwareDatetime
    saved_file: NotRequired[SavedFile]
