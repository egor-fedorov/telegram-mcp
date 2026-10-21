"""Opaque process-local cursors: immutable checkpoints, expiry and bounded storage."""

import copy
import pickle
import secrets
from collections import OrderedDict
from dataclasses import dataclass

from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import ToolError


@dataclass
class Entry:
    kind: str
    fingerprint: str
    expires: float
    value: object
    size: int


class CursorStore:
    def __init__(
        self, clock: Clock, ttl: int = 900, maximum: int = 128, max_bytes: int = 64 * 1024 * 1024
    ):
        self.clock = clock
        self.ttl = ttl
        self.maximum = maximum
        self.max_bytes = max_bytes
        self.entries: OrderedDict[str, Entry] = OrderedDict()

    def put(self, kind: str, fingerprint: str, value: object) -> str:
        now = self.clock.monotonic()
        self.entries = OrderedDict((k, v) for k, v in self.entries.items() if v.expires > now)
        token = secrets.token_urlsafe(24)
        # Serialize only our own objects to estimate size; never unpickle user input.
        size = len(pickle.dumps(value, protocol=5))
        if size > self.max_bytes:
            raise ToolError("QUERY_TOO_LARGE", "Narrow the selected folders/chats for this query.")
        self.entries[token] = Entry(kind, fingerprint, now + self.ttl, copy.deepcopy(value), size)
        while (
            len(self.entries) > self.maximum
            or sum(e.size for e in self.entries.values()) > self.max_bytes
        ):
            self.entries.popitem(last=False)
        return token

    def get(self, token: str, kind: str, fingerprint: str) -> object:
        entry = self.entries.get(token)
        if entry is None or entry.expires <= self.clock.monotonic():
            raise ToolError(
                "CURSOR_EXPIRED", "Restart the query; the cursor expired or server restarted."
            )
        if entry.kind != kind or entry.fingerprint != fingerprint:
            raise ToolError(
                "CURSOR_MISMATCH", "Repeat the original query parameters with this cursor."
            )
        return copy.deepcopy(entry.value)
