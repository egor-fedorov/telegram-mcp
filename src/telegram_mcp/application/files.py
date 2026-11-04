"""Immutable reusable files with fresh authorization of Telegram provenance."""

from telegram_mcp.application.attachments import AttachmentService, parse_attachment_id
from telegram_mcp.application.outbound_access import OutboundContext
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.outbound import FileList, RegisteredFile


class FileService:
    def __init__(self, context: OutboundContext, attachments: AttachmentService):
        self.context = context
        self.attachments = attachments

    def _enabled(self) -> None:
        policy = self.context.policies()
        if not (policy.allow_drafts or policy.allow_send_files):
            raise ToolError(
                "WRITES_DISABLED", "Enable allow_drafts or allow_send_files to register files."
            )

    async def register(
        self, attachment_id: str | None = None, path: str | None = None
    ) -> RegisteredFile:
        self._enabled()
        if (attachment_id is None) == (path is None):
            raise ToolError("INVALID_ARGUMENT", "Supply exactly one of attachment_id and path.")
        account = await self.context.backend.account_id()
        if path is not None:
            return await self.context.store.import_local(
                account, path, self.context.clock.utc_now()
            )
        assert attachment_id is not None
        stored, data = await self.attachments.extraction_input(attachment_id)
        await self.attachments.revalidate(stored)
        self._enabled()
        return await self.context.store.register(
            account,
            stored.result.name,
            stored.result.mime_type,
            data,
            self.context.clock.utc_now(),
            attachment_id,
            stored.identity,
        )

    async def list(self) -> FileList:
        self._enabled()
        account = await self.context.backend.account_id()
        # Do not expose metadata from sources whose access has since been revoked.
        visible = []
        for file in await self.context.store.files(account):
            try:
                await self._authorize(file)
            except ToolError:
                continue
            visible.append(file)
        return FileList(files=visible)

    async def read(self, account: str, file_id: str) -> tuple[RegisteredFile, bytes]:
        self._enabled()
        metadata, data = await self.context.store.file(account, file_id)
        await self._authorize(metadata)
        return metadata, data

    async def _authorize(self, file: RegisteredFile) -> None:
        if file.attachment_id is None:
            return
        self.attachments.ensure_enabled()
        ref, kind = parse_attachment_id(file.attachment_id)
        context = await self.context.access.load(explicit=True)
        await context.allow_explicit(ref.chat_id)
        source = await self.attachments.backend.prepare_attachment(ref, kind)
        if source.identity != file.source_identity:
            raise ToolError("FILE_UNAVAILABLE", "The original Telegram attachment has changed.")
