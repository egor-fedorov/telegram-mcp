"""Bounded temporary attachments, owned by one process per Telegram session."""

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

from telegram_mcp.adapters.private_paths import protect_private_path
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


@dataclass(frozen=True)
class _Entry:
    stored: StoredAttachment
    path: Path
    expires: float


class DownloadStore:
    def __init__(self, session_path: Path, clock: Clock, *, root: Path | None = None):
        session_key = hashlib.sha256(os.fsencode(session_path.resolve())).hexdigest()
        requested_root = (
            root or user_cache_path("telegram-mcp") / "attachments" / session_key
        ).absolute()
        # Canonicalize platform aliases such as macOS /var, but retain the leaf so
        # a redirected store directory itself is still rejected before access.
        self.root = requested_root.parent.resolve() / requested_root.name
        self._clock = clock
        self._lease: BinaryIO | None = None
        self._writers: set[_BoundedWriter] = set()
        self._entries: dict[str, _Entry] = {}
        self._orphans: dict[Path, int] = {}
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    def _start(self) -> None:
        if self._closed:
            raise _io_error()
        if self._lease is not None:
            return
        stream: BinaryIO | None = None
        locked = False
        try:
            # Reject redirected paths before creating or cleaning any private files.
            for path in (self.root, *self.root.parents):
                if path.is_symlink() or path.is_junction():
                    raise OSError("Temporary storage cannot use redirected paths")
            missing = []
            path = self.root
            while not path.exists():
                missing.append(path)
                path = path.parent
            for path in reversed(missing):
                path.mkdir(mode=0o700, exist_ok=True)
                protect_private_path(path, directory=True)
            if not missing:
                protect_private_path(self.root, directory=True)
            lock_path = self.root / ".lock"
            if lock_path.is_symlink() or lock_path.is_junction():
                raise OSError("Invalid storage lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            stream = os.fdopen(fd, "r+b")
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
            locked = True
            protect_private_path(lock_path)
            leftovers = {
                path: path.lstat().st_size
                for path in self.root.iterdir()
                if _MANAGED_NAME.fullmatch(path.name)
            }
            self._orphans.update(leftovers)
            self._sweep_files()
        except portalocker.exceptions.AlreadyLocked:
            if stream is not None:
                stream.close()
            raise ToolError(
                "SESSION_IN_USE", "Another process owns attachment storage for this session."
            ) from None
        except (OSError, portalocker.exceptions.LockException):
            if stream is not None:
                if locked:
                    with suppress(portalocker.exceptions.LockException):
                        portalocker.unlock(stream)
                stream.close()
            raise _io_error() from None
        self._lease = stream
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def reserve(self, source: AttachmentSource) -> AttachmentWriter:
        if source.size is not None and source.size > MAX_ATTACHMENT_BYTES:
            raise _too_large()
        async with self._lock:
            self._start()
            self._sweep_files()
            size = (
                sum(entry.stored.result.size for entry in self._entries.values())
                + sum(self._orphans.values())
                + len(self._writers) * MAX_ATTACHMENT_BYTES
            )
            count = len(self._entries) + len(self._orphans) + len(self._writers)
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
                    self._orphans[path] = 0
                    self._remove_orphans()
                raise _io_error() from None
            writer = _BoundedWriter(path, stream)
            self._writers.add(writer)
            return writer

    async def commit(self, writer: AttachmentWriter, source: AttachmentSource) -> StoredAttachment:
        async with self._lock:
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
            self._entries[path.stem] = _Entry(
                stored, path, self._clock.monotonic() + DOWNLOAD_TTL_SECONDS
            )
            self._writers.remove(actual)
            return stored

    def _owned_writer(self, writer: AttachmentWriter) -> _BoundedWriter:
        if not isinstance(writer, _BoundedWriter) or writer not in self._writers:
            raise _io_error()
        return writer

    async def discard(self, writer: AttachmentWriter) -> None:
        async with self._lock:
            if isinstance(writer, _BoundedWriter) and writer in self._writers:
                self._discard_writer(writer)

    async def discard_resource(self, token: str) -> None:
        async with self._lock:
            entry = self._entries.pop(token, None)
            if entry is not None:
                self._orphans[entry.path] = entry.stored.result.size
                self._remove_orphans()

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
        self._orphans[writer.path] = size
        self._remove_orphans()

    def _entry(self, token: str) -> _Entry:
        entry = self._entries.get(token)
        if self._closed or entry is None or self._clock.monotonic() >= entry.expires:
            raise _unavailable()
        return entry

    async def lookup(self, token: str) -> StoredAttachment:
        async with self._lock:
            return self._entry(token).stored

    async def read(self, token: str) -> bytes:
        async with self._lock:
            entry = self._entry(token)
            try:
                if entry.path.is_symlink() or entry.path.is_junction():
                    raise OSError("Invalid download path")
                fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    data = stream.read(MAX_ATTACHMENT_BYTES + 1)
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

    def _remove_orphans(self) -> None:
        for path in list(self._orphans):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            del self._orphans[path]

    def _sweep_files(self) -> None:
        for token, entry in list(self._entries.items()):
            if self._clock.monotonic() >= entry.expires:
                self._orphans[entry.path] = entry.stored.result.size
                del self._entries[token]
        self._remove_orphans()

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(DOWNLOAD_SWEEP_SECONDS)
            async with self._lock:
                self._sweep_files()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            if self._sweeper is not None:
                self._sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await self._sweeper
                self._sweeper = None
            for writer in list(self._writers):
                self._discard_writer(writer)
            for entry in self._entries.values():
                self._orphans[entry.path] = entry.stored.result.size
            self._entries.clear()
            self._remove_orphans()
            if self._lease is not None:
                try:
                    portalocker.unlock(self._lease)
                finally:
                    self._lease.close()
                    self._lease = None
