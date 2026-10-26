"""Permanent opt-in copies under one configured root; no expiry or automatic purge."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from pathlib import Path

from telegram_mcp.adapters.private_paths import prepare_private_directory, protect_private_path
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.models import SavedFile, ToolError

_KEY = re.compile(r"[A-Za-z0-9_-]{43}")
_MAX_BYTES = 20 * 1024 * 1024


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise OSError("Archived copy exceeds the file bound")
    return data


class ArchiveStore:
    def __init__(self, root: Path | None, clock: Clock):
        self.root = root.expanduser().absolute() if root is not None else None
        self.clock = clock
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telegram-archive")
        self._slots = asyncio.Semaphore(2)
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def require_enabled(self) -> None:
        if self.root is None:
            raise ToolError(
                "ARCHIVE_DISABLED",
                "Set archive_directory in local configuration before using save=true.",
            )

    async def _run[T](
        self, operation: Callable[[], T], rollback: Callable[[T], None] | None = None
    ) -> T:
        if self._closed:
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Archive storage is closed.")
        self._active += 1
        self._idle.clear()
        try:
            async with self._slots:
                future = asyncio.get_running_loop().run_in_executor(self._workers, operation)
                try:
                    return await asyncio.shield(future)
                except asyncio.CancelledError:
                    # Cancelling the waiter cannot stop fsync. Keep ownership until
                    # the worker finishes, then remove only this invocation's copy.
                    with suppress(Exception):
                        value = await self._settle(future)
                        if rollback is not None:
                            cleanup = asyncio.get_running_loop().run_in_executor(
                                self._workers, rollback, value
                            )
                            await self._settle(cleanup)
                    raise
        finally:
            self._active -= 1
            if not self._active:
                self._idle.set()

    @staticmethod
    async def _settle[T](future: asyncio.Future[T]) -> T:
        while not future.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(future)
        return future.result()

    async def save(
        self,
        data: bytes,
        name: str,
        sha256: str,
        *,
        mime_type: str,
        source: ArchiveSource,
        key: str | None = None,
    ) -> SavedFile:
        return await self._run(
            partial(self._save, data, name, sha256, mime_type, source, key), self._discard
        )

    def _save(
        self,
        data: bytes,
        name: str,
        sha256: str,
        mime_type: str,
        source: ArchiveSource,
        key: str | None = None,
    ) -> SavedFile:
        self.require_enabled()
        assert self.root is not None
        if len(data) > 20 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != sha256:
            raise ToolError("ARCHIVE_STORAGE_ERROR", "File size or checksum is inconsistent.")
        if key is not None and not _KEY.fullmatch(key):
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Invalid archive key.")
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ".bin"
        directory = self.root / (key or secrets.token_urlsafe(32))
        staging = self.root / ("." + directory.name + ".part")
        path = directory / ("file" + suffix)
        if key is not None:
            try:
                if directory.exists() or directory.is_symlink():
                    # A previous attempt published this key before its receipt was recorded.
                    return self._existing(path, sha256)
                if staging.exists() and not staging.is_symlink():
                    # Only this key's interrupted, never-published attempt can own it.
                    shutil.rmtree(staging)
            except OSError:
                raise ToolError(
                    "ARCHIVE_STORAGE_ERROR", "Cannot inspect the permanent copy location."
                ) from None
        saved_at = self.clock.utc_now()
        metadata = json.dumps(
            {
                "schema_version": 1,
                "file": path.name,
                "source": source.model_dump(mode="json", exclude_none=True),
                "original_name": name,
                "mime_type": mime_type,
                "size": len(data),
                "sha256": sha256,
                "saved_at": saved_at.isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        created = False
        try:
            prepare_private_directory(self.root)
            staging.mkdir(mode=0o700)
            created = True
            protect_private_path(staging, directory=True)
            for filename, content in ((path.name, data), ("metadata.json", metadata)):
                target = staging / filename
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    protect_private_path(target)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            # Publish the file and its description together. A random, nonempty
            # destination cannot overwrite another completed archive directory.
            staging.rename(directory)
            return SavedFile(
                local_path=str(path),
                metadata_path=str(directory / "metadata.json"),
                size=len(data),
                sha256=sha256,
                saved_at=saved_at,
            )
        except OSError:
            if created:
                with suppress(OSError):
                    shutil.rmtree(staging)
            raise ToolError(
                "ARCHIVE_STORAGE_ERROR",
                "Cannot save the permanent copy; check archive_directory permissions "
                "and disk space.",
            ) from None

    def _existing(self, path: Path, sha256: str) -> SavedFile:
        metadata = path.parent / "metadata.json"
        try:
            if path.parent.is_symlink() or path.is_symlink() or metadata.is_symlink():
                raise OSError("Redirected copy")
            manifest = json.loads(metadata.read_text(encoding="utf-8"))
            data = _read_bounded(path)
            if (
                manifest.get("sha256") != sha256
                or hashlib.sha256(data).hexdigest() != sha256
                or manifest.get("file") != path.name
            ):
                raise ValueError("Different copy")
            return SavedFile(
                local_path=str(path),
                metadata_path=str(metadata),
                size=len(data),
                sha256=sha256,
                saved_at=manifest["saved_at"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise ToolError(
                "ARCHIVE_COPY_INVALID",
                "The reserved permanent copy is missing, damaged or different.",
            ) from None

    async def verify(self, saved: SavedFile) -> bool:
        return await self._run(partial(self._verify, saved))

    @staticmethod
    def _verify(saved: SavedFile) -> bool:
        path, metadata = Path(saved.local_path), Path(saved.metadata_path)
        try:
            if path.is_symlink() or path.parent.is_symlink() or not metadata.is_file():
                return False
            data = _read_bounded(path)
        except OSError:
            return False
        return len(data) == saved.size and hashlib.sha256(data).hexdigest() == saved.sha256

    async def discard(self, saved: SavedFile) -> None:
        """Roll back only the copy created by an unsuccessful tool invocation."""
        await self._run(partial(self._discard, saved))

    def _discard(self, saved: SavedFile) -> None:
        path = Path(saved.local_path)
        metadata = Path(saved.metadata_path)
        if (
            self.root is None
            or path.parent.parent != self.root
            or metadata != path.parent / "metadata.json"
        ):
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Invalid archive rollback path.")
        try:
            if path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
                raise OSError("Redirected copy")
            path.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            raise ToolError(
                "ARCHIVE_STORAGE_ERROR", "Cannot remove the failed invocation's archive copy."
            ) from None

    async def close(self) -> None:
        self._closed = True
        await self._idle.wait()
        await asyncio.to_thread(self._workers.shutdown, wait=True)
