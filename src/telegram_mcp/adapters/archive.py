"""Permanent opt-in copies under one configured root; no expiry or automatic purge."""

import hashlib
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path

from telegram_mcp.adapters.private_paths import prepare_private_directory, protect_private_path
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import SavedFile, ToolError


class ArchiveStore:
    def __init__(self, root: Path | None, clock: Clock):
        self.root = root.expanduser().absolute() if root is not None else None
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def require_enabled(self) -> None:
        if self.root is None:
            raise ToolError(
                "ARCHIVE_DISABLED",
                "Set archive_directory in local configuration before using save=true.",
            )

    async def save(self, data: bytes, name: str, sha256: str) -> SavedFile:
        return self._save(data, name, sha256)

    def _save(self, data: bytes, name: str, sha256: str) -> SavedFile:
        self.require_enabled()
        assert self.root is not None
        if len(data) > 20 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != sha256:
            raise ToolError("ARCHIVE_STORAGE_ERROR", "File size or checksum is inconsistent.")
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            suffix = ".bin"
        directory = self.root / secrets.token_urlsafe(32)
        part, path = directory / ".part", directory / ("file" + suffix)
        created = False
        try:
            prepare_private_directory(self.root)
            directory.mkdir(mode=0o700)
            created = True
            protect_private_path(directory, directory=True)
            fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                protect_private_path(part)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # The destination is inside a newly created private directory, so
            # replacement cannot overwrite a previous copy, even across processes.
            part.replace(path)
            return SavedFile(
                local_path=str(path), size=len(data), sha256=sha256, saved_at=self.clock.utc_now()
            )
        except OSError:
            if created:
                with suppress(OSError):
                    part.unlink(missing_ok=True)
                    path.unlink(missing_ok=True)
                    directory.rmdir()
            raise ToolError(
                "ARCHIVE_STORAGE_ERROR",
                "Cannot save the permanent copy; check archive_directory permissions "
                "and disk space.",
            ) from None

    async def discard(self, saved: SavedFile) -> None:
        """Roll back only the copy created by an unsuccessful tool invocation."""
        self._discard(saved)

    def _discard(self, saved: SavedFile) -> None:
        path = Path(saved.local_path)
        if self.root is None or path.parent.parent != self.root:
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Invalid archive rollback path.")
        try:
            if path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
                raise OSError("Redirected copy")
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            raise ToolError(
                "ARCHIVE_STORAGE_ERROR", "Cannot remove the failed invocation's archive copy."
            ) from None
