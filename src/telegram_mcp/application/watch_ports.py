"""Storage of bounded subscriptions, including a durable unacknowledged batch."""

from typing import Protocol

from telegram_mcp.domain.watches import WatchState

MAX_WATCHES = 64
MAX_WATCH_BYTES = 64 * 1024 * 1024


class WatchStorePort(Protocol):
    def get(self, name: str, binding: str) -> WatchState | None: ...
    def names(self, binding: str, prefix: str, limit: int) -> list[str]:
        """Sorted names only; never loads batches or creates a missing store."""
        ...

    def list(self, binding: str) -> list[WatchState]: ...

    def save(self, state: WatchState, binding: str) -> None: ...
    def delete(self, name: str, binding: str) -> bool: ...
    def close(self) -> None: ...
