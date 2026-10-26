"""Explicit permanent copies, independent of temporary resource storage."""

from typing import Protocol

from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.models import SavedFile


class ArchivePort(Protocol):
    @property
    def enabled(self) -> bool: ...
    def require_enabled(self) -> None: ...
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
        """Publish a new copy; with key, reuse an identical copy already published there."""
        ...

    async def verify(self, saved: SavedFile) -> bool:
        """Report whether a recorded copy still exists with its recorded size and hash."""
        ...

    async def discard(self, saved: SavedFile) -> None: ...
    async def close(self) -> None: ...
