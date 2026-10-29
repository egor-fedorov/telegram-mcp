"""Private, expiring extraction snapshots with reserved capacity and verified reads."""

import asyncio
import hashlib
import os
import re
import secrets
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from telegram_mcp.adapters.private_paths import protect_private_path
from telegram_mcp.adapters.temporary_files import (
    StorageErrors,
    TemporaryEntry,
    TemporaryFiles,
    cache_root,
)
from telegram_mcp.application.attachment_ports import StoredAttachment
from telegram_mcp.application.extraction_ports import (
    EXTRACTION_TTL_SECONDS,
    MAX_EXTRACTION_BYTES,
    MAX_EXTRACTION_FILES,
    MAX_RESULT_BYTES,
    StoredExtraction,
)
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.extraction import ExtractedDocument
from telegram_mcp.domain.models import ToolError


def _expired() -> ToolError:
    return ToolError("EXTRACTION_EXPIRED", "This extraction is unknown or has expired.")


def _io_error() -> ToolError:
    return ToolError("LOCAL_IO_ERROR", "Cannot access temporary extraction storage.")


async def _finish_io[T](operation: Callable[[], T]) -> T:
    """Do not let cancelled writes outlive the reservation or cleanup lock."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        with suppress(Exception):
            task.result()
        raise


class ExtractionStore:
    def __init__(self, session_path: Path, clock: Clock, *, root: Path | None = None):
        self.root = cache_root("extractions", session_path, root)
        self.clock = clock
        self._reservations: set[str] = set()
        self._files: TemporaryFiles[StoredExtraction] = TemporaryFiles(
            self.root,
            clock,
            managed=re.compile(r"[A-Za-z0-9_-]{43}\.(?:part|json)\Z"),
            sweep_seconds=lambda: 60,
            errors=StorageErrors(
                io=_io_error,
                unavailable=_expired,
                closed=_expired,
                redirected=_io_error,
                in_use="Another process owns extraction storage for this session.",
            ),
        )

    async def reserve(self) -> str:
        async with self._files.lock:
            self._files.start()
            self._files.sweep()
            used, count = self._files.used
            reserved = len(self._reservations)
            if (
                used + (reserved + 1) * MAX_RESULT_BYTES > MAX_EXTRACTION_BYTES
                or count + reserved >= MAX_EXTRACTION_FILES
            ):
                raise ToolError(
                    "EXTRACTION_STORAGE_FULL",
                    "Temporary extraction storage is full. Retry after expiry.",
                )
            token = secrets.token_urlsafe(32)
            self._reservations.add(token)
            return token

    async def publish(
        self, token: str, source: StoredAttachment, document: ExtractedDocument
    ) -> StoredExtraction:
        async with self._files.lock:
            if token not in self._reservations or self._files.closed:
                raise _expired()
            data = document.model_dump_json().encode("utf-8")
            if len(data) > MAX_RESULT_BYTES:
                raise ToolError("EXTRACTION_LIMIT_EXCEEDED", "The extracted result exceeds 8 MiB.")
            path = self.root / f"{token}.json"
            part = self.root / f"{token}.part"
            # Account for both names before IO; cancellation may happen after rename.
            self._files.orphans[part] = len(data)
            self._files.orphans[path] = len(data)
            try:
                await _finish_io(lambda: self._write(part, path, data))
            except OSError:
                raise _io_error() from None
            result = StoredExtraction(
                source,
                hashlib.sha256(data).hexdigest(),
                self.clock.utc_now() + timedelta(seconds=EXTRACTION_TTL_SECONDS),
            )
            self._files.entries[token] = TemporaryEntry(
                result, path, len(data), self.clock.monotonic() + EXTRACTION_TTL_SECONDS
            )
            self._files.orphans.pop(part, None)
            self._files.orphans.pop(path, None)
            self._reservations.remove(token)
            return result

    @staticmethod
    def _write(part: Path, path: Path, data: bytes) -> None:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            protect_private_path(part)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        part.replace(path)

    async def lookup(self, token: str) -> StoredExtraction:
        async with self._files.lock:
            return self._files.entry(token).stored

    async def read(self, token: str) -> ExtractedDocument:
        async with self._files.lock:
            entry = self._files.entry(token)
            data = await _finish_io(
                lambda: self._files.read(entry, sha256=entry.stored.sha256, limit=MAX_RESULT_BYTES)
            )
            # Expiry is checked again after IO; reads never renew the TTL.
            self._files.entry(token)
            return ExtractedDocument.model_validate_json(data)

    async def discard(self, token: str) -> None:
        async with self._files.lock:
            self._reservations.discard(token)
            self._files.discard(token)
            self._files.remove_orphans()

    async def close(self) -> None:
        await self._files.close(before_cleanup=self._reservations.clear)
