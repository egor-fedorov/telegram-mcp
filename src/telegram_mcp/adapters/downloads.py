"""Bounded temporary attachments, owned by one process per Telegram session."""

import hashlib
import os
import re
import secrets
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from telegram_mcp.adapters.private_paths import protect_private_path
from telegram_mcp.adapters.temporary_files import (
    StorageErrors,
    TemporaryEntry,
    TemporaryFiles,
    cache_root,
)
from telegram_mcp.application.attachment_ports import (
    DOWNLOAD_SWEEP_SECONDS,
    DOWNLOAD_TTL_SECONDS,
    MAX_ATTACHMENT_BYTES,
    MAX_DOWNLOAD_BYTES,
    MAX_DOWNLOAD_FILES,
    RESOURCE_PREFIX,
    AttachmentSource,
    AttachmentWriter,
    StoredAttachment,
)
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import DownloadResult, ToolError

_MANAGED_NAME = re.compile(r"[A-Za-z0-9_-]{43}\.(?:part|bin)\Z")


def _io_error() -> ToolError:
    return ToolError(
        "LOCAL_IO_ERROR", "Cannot access temporary attachment storage. Check local permissions."
    )


def _unavailable() -> ToolError:
    return ToolError("DOWNLOAD_UNAVAILABLE", "This download is unknown or has expired.")


def _too_large() -> ToolError:
    return ToolError("ATTACHMENT_TOO_LARGE", "The attachment exceeds the 20 MiB download limit.")


class _BoundedWriter:
    def __init__(self, path: Path, stream: BinaryIO):
        self.path = path
        self.stream = stream
        self.size = 0

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_ATTACHMENT_BYTES:
            raise _too_large()
        try:
            written = self.stream.write(data)
            self.size = max(self.size, self.stream.tell())
            return written
        except (OSError, ValueError):
            raise _io_error() from None

    def seek(self, offset: int, whence: int = 0) -> int:
        try:
            target = offset + (self.tell() if whence == 1 else self.size if whence == 2 else 0)
            if target > MAX_ATTACHMENT_BYTES:
                raise _too_large()
            return self.stream.seek(offset, whence)
        except (OSError, ValueError):
            raise _io_error() from None

    def tell(self) -> int:
        try:
            return self.stream.tell()
        except (OSError, ValueError):
            raise _io_error() from None

    def truncate(self, size: int | None = None) -> int:
        actual = self.tell() if size is None else size
        if actual > MAX_ATTACHMENT_BYTES:
            raise _too_large()
        try:
            result = self.stream.truncate(actual)
            self.size = actual
            return result
        except (OSError, ValueError):
            raise _io_error() from None

    def flush(self) -> None:
        try:
            self.stream.flush()
        except (OSError, ValueError):
            raise _io_error() from None


class DownloadStore:
    def __init__(self, session_path: Path, clock: Clock, *, root: Path | None = None):
        self.root = cache_root("attachments", session_path, root)
        self._clock = clock
        self._writers: set[_BoundedWriter] = set()
        self._files: TemporaryFiles[StoredAttachment] = TemporaryFiles(
            self.root,
            clock,
            managed=_MANAGED_NAME,
            # Read at each pass, so the interval follows the module setting.
            sweep_seconds=lambda: DOWNLOAD_SWEEP_SECONDS,
            errors=StorageErrors(
                io=_io_error,
                unavailable=_unavailable,
                closed=_io_error,
                redirected=_io_error,
                in_use="Another process owns attachment storage for this session.",
            ),
        )

    async def reserve(self, source: AttachmentSource) -> AttachmentWriter:
        if source.size is not None and source.size > MAX_ATTACHMENT_BYTES:
            raise _too_large()
        files = self._files
        async with files.lock:
            files.start()
            files.sweep()
            used, count = files.used
            size = used + len(self._writers) * MAX_ATTACHMENT_BYTES
            count += len(self._writers)
            if size + MAX_ATTACHMENT_BYTES > MAX_DOWNLOAD_BYTES or count >= MAX_DOWNLOAD_FILES:
                raise ToolError(
                    "DOWNLOAD_STORAGE_FULL",
                    "Temporary attachment storage is full. Try again later.",
                )
            path = self.root / (secrets.token_urlsafe(32) + ".part")
            stream: BinaryIO | None = None
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                stream = os.fdopen(fd, "w+b")
                protect_private_path(path)
            except OSError:
                if stream is not None:
                    stream.close()
                    files.orphans[path] = 0
                    files.remove_orphans()
                raise _io_error() from None
            writer = _BoundedWriter(path, stream)
            self._writers.add(writer)
            return writer

    async def commit(self, writer: AttachmentWriter, source: AttachmentSource) -> StoredAttachment:
        async with self._files.lock:
            actual = self._owned_writer(writer)
            try:
                actual.flush()
                size = os.fstat(actual.stream.fileno()).st_size
                if size > MAX_ATTACHMENT_BYTES:
                    raise _too_large()
                actual.stream.seek(0)
                hasher = hashlib.sha256()
                read_size = 0
                while chunk := actual.stream.read(256 * 1024):
                    read_size += len(chunk)
                    if read_size > MAX_ATTACHMENT_BYTES:
                        raise _too_large()
                    hasher.update(chunk)
                digest = hasher.hexdigest()
                if read_size != size:
                    raise _io_error()
                actual.stream.close()
                path = actual.path.with_suffix(".bin")
                actual.path.replace(path)
                actual.path = path
            except (OSError, ValueError):
                raise _io_error() from None
            result = DownloadResult(
                attachment_id=source.attachment_id,
                message_ref=source.message_ref,
                name=source.name,
                mime_type=source.mime_type,
                size=size,
                sha256=digest,
                local_path=str(path),
                resource_uri=RESOURCE_PREFIX + path.stem,
                expires_at=self._clock.utc_now() + timedelta(seconds=DOWNLOAD_TTL_SECONDS),
            )
            stored = StoredAttachment(result=result, identity=source.identity)
            self._files.entries[path.stem] = TemporaryEntry(
                stored, path, size, self._clock.monotonic() + DOWNLOAD_TTL_SECONDS
            )
            self._writers.remove(actual)
            return stored

    def _owned_writer(self, writer: AttachmentWriter) -> _BoundedWriter:
        if not isinstance(writer, _BoundedWriter) or writer not in self._writers:
            raise _io_error()
        return writer

    async def discard(self, writer: AttachmentWriter) -> None:
        async with self._files.lock:
            if isinstance(writer, _BoundedWriter) and writer in self._writers:
                self._discard_writer(writer)

    async def discard_resource(self, token: str) -> None:
        async with self._files.lock:
            self._files.discard(token)
            self._files.remove_orphans()

    def _discard_writer(self, writer: _BoundedWriter) -> None:
        with suppress(OSError):
            writer.stream.close()
        self._writers.remove(writer)
        try:
            size = writer.path.lstat().st_size
        except FileNotFoundError:
            return
        except OSError:
            size = MAX_ATTACHMENT_BYTES
        self._files.orphans[writer.path] = size
        self._files.remove_orphans()

    async def lookup(self, token: str) -> StoredAttachment:
        async with self._files.lock:
            return self._files.entry(token).stored

    async def read(self, token: str) -> bytes:
        async with self._files.lock:
            entry = self._files.entry(token)
            return self._files.read(
                entry, sha256=entry.stored.result.sha256, limit=MAX_ATTACHMENT_BYTES
            )

    async def close(self) -> None:
        await self._files.close(before_cleanup=self._discard_writers)

    def _discard_writers(self) -> None:
        for writer in list(self._writers):
            self._discard_writer(writer)
