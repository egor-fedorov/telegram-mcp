"""Download explicitly selected media using fresh protection checks and public APIs."""

from dataclasses import dataclass
from typing import Any

from telethon import errors, types, utils

from telegram_mcp.adapters.telegram.runtime import TelegramRuntime
from telegram_mcp.application.attachment_ports import (
    DOWNLOAD_TIMEOUT_SECONDS,
    MAX_ATTACHMENT_BYTES,
    AttachmentKind,
    AttachmentSource,
    AttachmentWriter,
)
from telegram_mcp.domain.models import MessageRef, ToolError

_REFERENCE_ERRORS = (
    errors.FileReferenceExpiredError,
    errors.FileReferenceInvalidError,
    errors.FileReferenceEmptyError,
    errors.FilerefUpgradeNeededError,
)


@dataclass(frozen=True)
class _Media:
    value: types.Photo | types.Document
    thumb: types.PhotoSize | types.PhotoSizeProgressive | None = None


def _unavailable() -> ToolError:
    return ToolError("ATTACHMENT_UNAVAILABLE", "The selected attachment is unavailable.")


def _protected() -> ToolError:
    return ToolError(
        "ATTACHMENT_PROTECTED", "Telegram restricts saving this message or its attachment."
    )


def _size(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _photo_size(photo: types.Photo) -> types.PhotoSize | types.PhotoSizeProgressive:
    candidates = [
        size
        for size in photo.sizes
        if isinstance(size, (types.PhotoSize, types.PhotoSizeProgressive))
        and type(size.w) is int
        and size.w > 0
        and type(size.h) is int
        and size.h > 0
        and isinstance(size.type, str)
        and size.type
        and (
            isinstance(size, types.PhotoSize)
            or bool(size.sizes)
            and all(_size(value) is not None for value in size.sizes)
        )
    ]
    if not candidates:
        raise _unavailable()
    return max(candidates, key=lambda size: (size.w * size.h, max(size.w, size.h), size.type))


def _source(ref: MessageRef, kind: AttachmentKind, message: types.Message) -> AttachmentSource:
    media = message.media
    ttl = getattr(media, "ttl_seconds", None)
    if message.noforwards or type(ttl) is int and ttl > 0:
        raise _protected()
    if kind == "photo" and isinstance(media, types.MessageMediaPhoto):
        if not isinstance(media.photo, types.Photo):
            raise _unavailable()
        photo = media.photo
        thumb = _photo_size(photo)
        size = _size(thumb.size) if isinstance(thumb, types.PhotoSize) else max(thumb.sizes)
        identity = f"photo:{photo.id}:{thumb.type}"
        name, mime_type = f"photo-{ref.message_id}.jpg", "image/jpeg"
        payload = _Media(photo, thumb)
    elif kind == "document" and isinstance(media, types.MessageMediaDocument):
        if not isinstance(media.document, types.Document):
            raise _unavailable()
        document = media.document
        size = _size(document.size)
        identity = f"document:{document.id}"
        name = next(
            (
                attr.file_name
                for attr in document.attributes
                if isinstance(attr, types.DocumentAttributeFilename)
                and isinstance(attr.file_name, str)
                and attr.file_name
            ),
            f"document-{ref.message_id}.bin",
        )
        mime_type = document.mime_type or "application/octet-stream"
        payload = _Media(document)
    else:
        raise _unavailable()
    if size is not None and size > MAX_ATTACHMENT_BYTES:
        raise ToolError("ATTACHMENT_TOO_LARGE", "The selected attachment exceeds 20 MiB.")
    return AttachmentSource(
        attachment_id=f"{ref.key}:{kind}",
        message_ref=ref,
        kind=kind,
        identity=identity,
        name=name,
        mime_type=mime_type,
        size=size,
        payload=payload,
    )


class AttachmentGateway:
    def __init__(self, runtime: TelegramRuntime):
        self.runtime = runtime

    async def prepare_attachment(self, ref: MessageRef, kind: AttachmentKind) -> AttachmentSource:
        client = await self.runtime.ensure()
        return await self.runtime.call(self._prepare(client, ref, kind))

    async def _prepare(
        self, client: Any, ref: MessageRef, kind: AttachmentKind
    ) -> AttachmentSource:
        try:
            # get_entity requests fresh metadata; cached input entities cannot prove protection.
            entity = await client.get_entity(int(ref.chat_id))
            if (
                not isinstance(entity, (types.User, types.Chat, types.Channel))
                or str(utils.get_peer_id(entity)) != ref.chat_id
            ):
                raise _unavailable()
            if getattr(entity, "noforwards", False):
                raise _protected()
            rows = await client.get_messages(entity, ids=[ref.message_id])
        except (errors.MessageIdInvalidError, errors.ChannelPrivateError):
            raise _unavailable() from None
        if not isinstance(rows, list) or len(rows) != 1:
            raise _unavailable()
        raw = rows[0]
        if (
            not isinstance(raw, types.Message)
            or raw.id != ref.message_id
            or not isinstance(raw.peer_id, (types.PeerUser, types.PeerChat, types.PeerChannel))
            or str(utils.get_peer_id(raw.peer_id)) != ref.chat_id
        ):
            raise _unavailable()
        return _source(ref, kind, raw)

    async def download_attachment(self, source: AttachmentSource, writer: AttachmentWriter) -> None:
        client = await self.runtime.ensure()
        await self.runtime.call(
            self._download(client, source, writer), timeout_seconds=DOWNLOAD_TIMEOUT_SECONDS
        )

    async def _download(
        self, client: Any, source: AttachmentSource, writer: AttachmentWriter
    ) -> None:
        current = source
        for attempt in range(2):
            payload = current.payload
            if not isinstance(payload, _Media):
                raise _unavailable()
            try:
                # Passing only media prevents Telethon's own unchecked message refetch.
                result = await client.download_media(
                    payload.value,
                    file=writer,
                    thumb=payload.thumb.type if payload.thumb is not None else None,
                )
            except _REFERENCE_ERRORS:
                if attempt:
                    raise _unavailable() from None
                current = await self.prepare_attachment(source.message_ref, source.kind)
                if current.identity != source.identity:
                    raise _unavailable() from None
                writer.seek(0)
                writer.truncate(0)
            except errors.ChatForwardsRestrictedError:
                raise _protected() from None
            else:
                if result is None:
                    raise _unavailable()
                writer.flush()
                if current.size is not None and writer.tell() != current.size:
                    raise ToolError(
                        "ATTACHMENT_UNAVAILABLE",
                        "Telegram returned an unexpected attachment size. Retry the download.",
                    )
                return
