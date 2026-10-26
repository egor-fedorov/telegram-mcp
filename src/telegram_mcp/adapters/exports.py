"""Private temporary text exports, bounded independently of attachment downloads."""

import asyncio
import hashlib
import os
import re
import secrets
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

import portalocker
from platformdirs import user_cache_path

from telegram_mcp.adapters.private_paths import prepare_private_directory, protect_private_path
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


@dataclass(frozen=True)
class _Entry:
    stored: StoredExport
    path: Path
    expires: float


class ExportStore:
    def __init__(self, session_path: Path, clock: Clock, *, root: Path | None = None):
        key = hashlib.sha256(os.fsencode(session_path.resolve())).hexdigest()
        requested = (root or user_cache_path("telegram-mcp") / "exports" / key).absolute()
        self.root = requested.parent.resolve() / requested.name
        self.clock = clock
        self._entries: dict[str, _Entry] = {}
        self._orphans: dict[Path, int] = {}
        self._lease: BinaryIO | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    def _start(self) -> None:
        if self._closed:
            raise _unavailable()
        if self._lease is not None:
            return
        stream = None
        try:
            prepare_private_directory(self.root)
            path = self.root / ".lock"
            if path.is_symlink() or path.is_junction():
                raise OSError("Redirected lock")
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            stream = os.fdopen(fd, "r+b")
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
            protect_private_path(path)
            self._orphans.update(
                {
                    path: path.lstat().st_size
                    for path in self.root.iterdir()
                    if _NAME.fullmatch(path.name)
                }
            )
            self._sweep()
        except portalocker.exceptions.AlreadyLocked:
            if stream is not None:
                stream.close()
            raise ToolError(
                "SESSION_IN_USE", "Another process owns this session's export storage."
            ) from None
        except (OSError, portalocker.exceptions.LockException):
            if stream is not None:
                stream.close()
            raise _io_error() from None
        self._lease = stream
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def save(
        self, data: bytes, format: ExportFormat, chat_id: str, binding: str
    ) -> ExportFile:
        if len(data) > MAX_EXPORT_BYTES:
            raise ToolError("EXPORT_TOO_LARGE", "Export parts are limited to 20 MiB.")
        async with self._lock:
            self._start()
            self._sweep()
            used = sum(e.stored.result.size for e in self._entries.values()) + sum(
                self._orphans.values()
            )
            if (
                used + len(data) > MAX_EXPORT_STORE_BYTES
                or len(self._entries) + len(self._orphans) >= MAX_EXPORT_FILES
            ):
                raise ToolError(
                    "EXPORT_STORAGE_FULL", "Temporary export storage is full; retry after expiry."
                )
            token = secrets.token_urlsafe(32)
            part = self.root / (token + ".part")
            path = self.root / (token + (".jsonl" if format == "jsonl" else ".md"))
            try:
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self._orphans[part] = len(data)
                with os.fdopen(fd, "wb") as stream:
                    protect_private_path(part)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                part.replace(path)
                del self._orphans[part]
                self._orphans[path] = len(data)
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
                self._entries[token] = _Entry(
                    StoredExport(result, chat_id, binding),
                    path,
                    self.clock.monotonic() + EXPORT_TTL_SECONDS,
                )
                del self._orphans[path]
                return result
            except OSError:
                self._sweep()
                raise _io_error() from None

    def _entry(self, uri: str) -> _Entry:
        token = uri.removeprefix(EXPORT_RESOURCE_PREFIX)
        if not uri.startswith(EXPORT_RESOURCE_PREFIX) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", token
        ):
            raise _unavailable()
        entry = self._entries.get(token)
        if self._closed or entry is None or self.clock.monotonic() >= entry.expires:
            raise _unavailable()
        return entry

    async def lookup(self, uri: str) -> StoredExport:
        async with self._lock:
            return self._entry(uri).stored

    async def read(self, uri: str) -> bytes:
        async with self._lock:
            entry = self._entry(uri)
            try:
                if entry.path.is_symlink() or entry.path.is_junction():
                    raise _unavailable()
                fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    data = stream.read(MAX_EXPORT_BYTES + 1)
                if (
                    len(data) != entry.stored.result.size
                    or hashlib.sha256(data).hexdigest() != entry.stored.result.sha256
                ):
                    raise _unavailable()
                return data
            except FileNotFoundError:
                raise _unavailable() from None
            except OSError:
                raise _io_error() from None

    async def discard(self, uri: str) -> None:
        async with self._lock:
            entry = self._entries.pop(uri.removeprefix(EXPORT_RESOURCE_PREFIX), None)
            if entry is not None:
                self._orphans[entry.path] = entry.stored.result.size
            self._sweep()

    def _sweep(self) -> None:
        for token, entry in list(self._entries.items()):
            if self.clock.monotonic() >= entry.expires:
                self._orphans[entry.path] = entry.stored.result.size
                del self._entries[token]
        for path in list(self._orphans):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            del self._orphans[path]

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            async with self._lock:
                self._sweep()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            if self._sweeper is not None:
                self._sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await self._sweeper
                self._sweeper = None
            for entry in self._entries.values():
                self._orphans[entry.path] = entry.stored.result.size
            self._entries.clear()
            self._sweep()
            if self._lease is not None:
                self._lease.close()
                self._lease = None
