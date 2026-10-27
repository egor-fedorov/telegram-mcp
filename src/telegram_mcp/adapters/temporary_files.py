"""A session-owned private directory of expiring temporary files.

Download and export stores share the process lease, expiring entries, cleanup of
leftover managed files and verified bounded reads. Each store keeps its own file
names, limits, write path and error messages.
"""

import asyncio
import hashlib
import os
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import portalocker
from platformdirs import user_cache_path

from telegram_mcp.adapters.private_paths import prepare_private_directory, protect_private_path
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import ToolError


def cache_root(kind: str, session_path: Path, root: Path | None) -> Path:
    """Per-session cache directory, canonicalized without resolving its own leaf."""
    key = hashlib.sha256(os.fsencode(session_path.resolve())).hexdigest()
    requested = (root or user_cache_path("telegram-mcp") / kind / key).absolute()
    # Canonicalize platform aliases such as macOS /var, but retain the leaf so
    # a redirected store directory itself is still rejected before access.
    return requested.parent.resolve() / requested.name


@dataclass(frozen=True)
class StorageErrors:
    io: Callable[[], ToolError]
    unavailable: Callable[[], ToolError]
    closed: Callable[[], ToolError]
    redirected: Callable[[], ToolError]
    in_use: str


@dataclass(frozen=True)
class TemporaryEntry[T]:
    stored: T
    path: Path
    size: int
    expires: float


class TemporaryFiles[T]:
    """Lease, expiry and cleanup; callers hold `lock` around every operation."""

    def __init__(
        self,
        root: Path,
        clock: Clock,
        *,
        managed: re.Pattern[str],
        sweep_seconds: Callable[[], float],
        errors: StorageErrors,
    ):
        self.root, self.clock = root, clock
        self.managed, self.sweep_seconds, self.errors = managed, sweep_seconds, errors
        self.entries: dict[str, TemporaryEntry[T]] = {}
        self.orphans: dict[Path, int] = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self._lease: BinaryIO | None = None
        self._sweeper: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Take the exclusive lease once, then remove leftovers of earlier processes."""
        if self.closed:
            raise self.errors.closed()
        if self._lease is not None:
            return
        stream: BinaryIO | None = None
        locked = False
        try:
            prepare_private_directory(self.root)
            path = self.root / ".lock"
            if path.is_symlink() or path.is_junction():
                raise OSError("Redirected lock")
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            stream = os.fdopen(fd, "r+b")
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
            locked = True
            protect_private_path(path)
            self.orphans.update(
                {
                    leftover: leftover.lstat().st_size
                    for leftover in self.root.iterdir()
                    if self.managed.fullmatch(leftover.name)
                }
            )
            self.sweep()
        except portalocker.exceptions.AlreadyLocked:
            if stream is not None:
                stream.close()
            raise ToolError("SESSION_IN_USE", self.errors.in_use) from None
        except (OSError, portalocker.exceptions.LockException):
            if stream is not None:
                if locked:
                    with suppress(portalocker.exceptions.LockException):
                        portalocker.unlock(stream)
                stream.close()
            raise self.errors.io() from None
        self._lease = stream
        self._sweeper = asyncio.create_task(self._sweep_loop())

    @property
    def used(self) -> tuple[int, int]:
        """Bytes and files held by published entries and not yet removed leftovers."""
        size = sum(entry.size for entry in self.entries.values()) + sum(self.orphans.values())
        return size, len(self.entries) + len(self.orphans)

    def entry(self, token: str) -> TemporaryEntry[T]:
        entry = self.entries.get(token)
        if self.closed or entry is None or self.clock.monotonic() >= entry.expires:
            raise self.errors.unavailable()
        return entry

    def discard(self, token: str) -> None:
        entry = self.entries.pop(token, None)
        if entry is not None:
            self.orphans[entry.path] = entry.size

    def read(self, entry: TemporaryEntry[T], *, sha256: str, limit: int) -> bytes:
        """Return the published bytes only if they still match their recorded digest."""
        try:
            if entry.path.is_symlink() or entry.path.is_junction():
                raise self.errors.redirected()
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(limit + 1)
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != sha256:
                raise self.errors.unavailable()
            return data
        except FileNotFoundError:
            raise self.errors.unavailable() from None
        except OSError:
            raise self.errors.io() from None

    def sweep(self) -> None:
        for token, entry in list(self.entries.items()):
            if self.clock.monotonic() >= entry.expires:
                self.orphans[entry.path] = entry.size
                del self.entries[token]
        self.remove_orphans()

    def remove_orphans(self) -> None:
        for path in list(self.orphans):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            del self.orphans[path]

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_seconds())
            async with self.lock:
                self.sweep()

    async def close(self, before_cleanup: Callable[[], None] | None = None) -> None:
        """Stop the sweeper, remove every file of this process and release the lease."""
        async with self.lock:
            self.closed = True
            if self._sweeper is not None:
                self._sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await self._sweeper
                self._sweeper = None
            if before_cleanup is not None:
                before_cleanup()
            for entry in self.entries.values():
                self.orphans[entry.path] = entry.size
            self.entries.clear()
            self.remove_orphans()
            if self._lease is not None:
                try:
                    portalocker.unlock(self._lease)
                finally:
                    self._lease.close()
                    self._lease = None
