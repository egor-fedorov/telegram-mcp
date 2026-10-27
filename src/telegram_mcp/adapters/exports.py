"""Private temporary text exports, bounded independently of attachment downloads."""

import hashlib
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path

from telegram_mcp.adapters.private_paths import protect_private_path
from telegram_mcp.adapters.temporary_files import (
    StorageErrors,
    TemporaryEntry,
    TemporaryFiles,
    cache_root,
)
from telegram_mcp.application.change_ports import (
    EXPORT_RESOURCE_PREFIX,
    EXPORT_TTL_SECONDS,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_FILES,
    MAX_EXPORT_STORE_BYTES,
    StoredExport,
)
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.changes import ExportFile, ExportFormat
from telegram_mcp.domain.models import ToolError

_NAME = re.compile(r"[A-Za-z0-9_-]{43}\.(?:part|jsonl|md)\Z")


def _unavailable() -> ToolError:
    return ToolError("EXPORT_UNAVAILABLE", "The export is unknown, changed or has expired.")


def _io_error() -> ToolError:
    return ToolError("LOCAL_IO_ERROR", "Cannot access temporary export storage.")


class ExportStore:
    def __init__(self, session_path: Path, clock: Clock, *, root: Path | None = None):
        self.root = cache_root("exports", session_path, root)
        self.clock = clock
        self._files: TemporaryFiles[StoredExport] = TemporaryFiles(
            self.root,
            clock,
            managed=_NAME,
            sweep_seconds=lambda: 60,
            errors=StorageErrors(
                io=_io_error,
                unavailable=_unavailable,
                closed=_unavailable,
                redirected=_unavailable,
                in_use="Another process owns this session's export storage.",
            ),
        )

    async def save(
        self, data: bytes, format: ExportFormat, chat_id: str, binding: str
    ) -> ExportFile:
        if len(data) > MAX_EXPORT_BYTES:
            raise ToolError("EXPORT_TOO_LARGE", "Export parts are limited to 20 MiB.")
        files = self._files
        async with files.lock:
            files.start()
            files.sweep()
            used, count = files.used
            if used + len(data) > MAX_EXPORT_STORE_BYTES or count >= MAX_EXPORT_FILES:
                raise ToolError(
                    "EXPORT_STORAGE_FULL", "Temporary export storage is full; retry after expiry."
                )
            token = secrets.token_urlsafe(32)
            part = self.root / (token + ".part")
            path = self.root / (token + (".jsonl" if format == "jsonl" else ".md"))
            try:
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                files.orphans[part] = len(data)
                with os.fdopen(fd, "wb") as stream:
                    protect_private_path(part)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                part.replace(path)
                del files.orphans[part]
                files.orphans[path] = len(data)
                result = ExportFile(
                    format=format,
                    name=f"telegram-{chat_id}{path.suffix}",
                    mime_type="application/x-ndjson" if format == "jsonl" else "text/markdown",
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    local_path=str(path),
                    resource_uri=EXPORT_RESOURCE_PREFIX + token,
                    expires_at=self.clock.utc_now() + timedelta(seconds=EXPORT_TTL_SECONDS),
                )
                files.entries[token] = TemporaryEntry(
                    StoredExport(result, chat_id, binding),
                    path,
                    len(data),
                    self.clock.monotonic() + EXPORT_TTL_SECONDS,
                )
                del files.orphans[path]
                return result
            except OSError:
                files.sweep()
                raise _io_error() from None

    @staticmethod
    def _token(uri: str) -> str:
        token = uri.removeprefix(EXPORT_RESOURCE_PREFIX)
        if not uri.startswith(EXPORT_RESOURCE_PREFIX) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", token
        ):
            raise _unavailable()
        return token

    async def lookup(self, uri: str) -> StoredExport:
        async with self._files.lock:
            return self._files.entry(self._token(uri)).stored

    async def read(self, uri: str) -> bytes:
        async with self._files.lock:
            entry = self._files.entry(self._token(uri))
            return self._files.read(
                entry, sha256=entry.stored.result.sha256, limit=MAX_EXPORT_BYTES
            )

    async def discard(self, uri: str) -> None:
        async with self._files.lock:
            self._files.discard(uri.removeprefix(EXPORT_RESOURCE_PREFIX))
            self._files.sweep()

    async def close(self) -> None:
        await self._files.close()
