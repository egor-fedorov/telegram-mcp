"""A credential-free snapshot of the current retrieval policy."""

from dataclasses import dataclass

# Telegram's service-notification account delivers login codes; it is never readable.
SERVICE_NOTIFICATIONS_ID = "777000"


@dataclass(frozen=True)
class ReadPolicy:
    allowed_chat_ids: tuple[str, ...] = ()
    allowed_folder_ids: tuple[int, ...] = ()
    allow_all_chats: bool = False
    allow_public_channel_reads: bool = False
    allow_public_channel_discovery: bool = False
    allow_attachment_downloads: bool = False

    def allows_folder(self, folder_id: int) -> bool:
        return self.allow_all_chats or folder_id in self.allowed_folder_ids
