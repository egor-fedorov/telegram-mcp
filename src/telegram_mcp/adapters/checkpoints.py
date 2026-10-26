"""Private SQLite metadata snapshots; never deserialize executable Python objects."""

import hashlib
import os
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platformdirs import user_data_path
from pydantic import ValidationError

from telegram_mcp.adapters.private_paths import prepare_private_directory, protect_private_path
from telegram_mcp.application.change_ports import (
    CHECKPOINT_TTL_SECONDS,
    MAX_CHECKPOINT_BYTES,
    MAX_CHECKPOINTS,
    CheckpointReceipt,
)
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.changes import CheckpointState
from telegram_mcp.domain.models import ToolError


def _storage_error() -> ToolError:
    return ToolError("CHECKPOINT_STORAGE_ERROR", "Cannot read or write local checkpoint storage.")


class CheckpointStore:
    def __init__(
        self,
        session_path: Path,
        clock: Clock,
        *,
        root: Path | None = None,
        maximum: int = MAX_CHECKPOINTS,
        max_bytes: int = MAX_CHECKPOINT_BYTES,
    ):
        key = hashlib.sha256(os.fsencode(session_path.resolve())).hexdigest()
        requested = (root or user_data_path("telegram-mcp") / "checkpoints" / key).absolute()
        self.root = requested.parent.resolve() / requested.name
        self.path = self.root / "checkpoints.sqlite3"
        self.clock = clock
        self.maximum, self.max_bytes = maximum, max_bytes
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    def _db(self) -> sqlite3.Connection:
        if self._closed:
            raise _storage_error()
        if self._connection is not None:
            return self._connection
        connection = None
        try:
            prepare_private_directory(self.root)
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = Path(str(self.path) + suffix)
                if candidate.is_symlink() or candidate.is_junction():
                    raise OSError("Redirected database file")
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            protect_private_path(self.path)
            connection = sqlite3.connect(self.path, timeout=1)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ToolError("CHECKPOINT_INCOMPATIBLE", "Unknown checkpoint database version.")
            if version == 0:
                if connection.execute("SELECT name FROM sqlite_master").fetchone():
                    raise _storage_error()
                connection.execute("PRAGMA auto_vacuum = FULL")
                with connection:
                    connection.execute(
                        "CREATE TABLE checkpoints (token TEXT PRIMARY KEY, binding TEXT NOT NULL, "
                        "kind TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL, "
                        "size INTEGER NOT NULL)"
                    )
                    connection.execute(
                        "CREATE TABLE cooldowns (binding TEXT PRIMARY KEY, until REAL NOT NULL)"
                    )
                    connection.execute("PRAGMA user_version = 1")
            self._connection = connection
            return connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            raise _storage_error() from None
        except ToolError:
            if connection is not None:
                connection.close()
            raise

    def get(self, token: str, binding: str, kind: str) -> CheckpointState:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ToolError("INVALID_CHECKPOINT", "Invalid checkpoint token.")
        try:
            row = (
                self._db()
                .execute(
                    "SELECT binding, kind, expires, payload FROM checkpoints WHERE token = ?",
                    (token,),
                )
                .fetchone()
            )
            if row is None or row[2] <= self.clock.utc_now().timestamp():
                raise ToolError(
                    "CHECKPOINT_EXPIRED",
                    "Checkpoint expired or was evicted; explicitly start again.",
                )
            if row[0] != binding or row[1] != kind:
                raise ToolError(
                    "CHECKPOINT_MISMATCH", "Checkpoint belongs to another session or tool."
                )
            state = CheckpointState.model_validate_json(row[3])
            if state.kind != kind:
                raise ToolError("CHECKPOINT_INCOMPATIBLE", "Checkpoint kind is inconsistent.")
            return state
        except ValidationError:
            raise ToolError(
                "CHECKPOINT_INCOMPATIBLE", "Unsupported or damaged checkpoint state."
            ) from None
        except sqlite3.Error:
            raise _storage_error() from None

    def put(
        self, state: CheckpointState, binding: str, *, retain: str | None = None
    ) -> CheckpointReceipt:
        try:
            payload = CheckpointState.model_validate(state.model_dump()).model_dump_json()
        except ValidationError:
            raise _storage_error() from None
        size = len(payload.encode("utf-8"))
        if size > self.max_bytes:
            raise _storage_error()
        now = self.clock.utc_now()
        expires = now + timedelta(seconds=CHECKPOINT_TTL_SECONDS)
        token = secrets.token_urlsafe(32)
        try:
            db = self._db()
            with db:
                db.execute("DELETE FROM checkpoints WHERE expires <= ?", (now.timestamp(),))
                db.execute("DELETE FROM cooldowns WHERE until <= ?", (now.timestamp(),))
                count, used = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM checkpoints"
                ).fetchone()
                while count >= self.maximum or used + size > self.max_bytes:
                    oldest = db.execute(
                        "SELECT token, size FROM checkpoints WHERE token != ? "
                        "ORDER BY rowid LIMIT 1",
                        (retain or "",),
                    ).fetchone()
                    if oldest is None:
                        raise _storage_error()
                    db.execute("DELETE FROM checkpoints WHERE token = ?", (oldest[0],))
                    count, used = count - 1, used - oldest[1]
                db.execute(
                    "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                    (token, binding, state.kind, expires.timestamp(), payload, size),
                )
        except sqlite3.Error:
            raise _storage_error() from None
        return CheckpointReceipt(token, expires)

    def retry_at(self, binding: str) -> datetime | None:
        try:
            row = (
                self._db()
                .execute("SELECT until FROM cooldowns WHERE binding = ?", (binding,))
                .fetchone()
            )
            return datetime.fromtimestamp(row[0], UTC) if row else None
        except (sqlite3.Error, ValueError, OverflowError):
            raise _storage_error() from None

    def defer(self, binding: str, until: datetime) -> None:
        try:
            db = self._db()
            with db:
                db.execute(
                    "INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(binding) "
                    "DO UPDATE SET until = MAX(cooldowns.until, excluded.until)",
                    (binding, until.timestamp()),
                )
        except sqlite3.Error:
            raise _storage_error() from None

    def close(self) -> None:
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            self._connection = None
