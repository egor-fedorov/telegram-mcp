"""Atomic private subscription snapshots; never evict a consumer or its delivery."""

import hashlib
import os
import sqlite3
from pathlib import Path

from platformdirs import user_data_path
from pydantic import ValidationError

from telegram_mcp.adapters.state_database import open_private_database
from telegram_mcp.application.watch_ports import MAX_WATCH_BYTES, MAX_WATCHES
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.watches import WatchState


def storage_error() -> ToolError:
    return ToolError("WATCH_STORAGE_ERROR", "Cannot read or write local subscription storage.")


class WatchStore:
    def __init__(
        self,
        session_path: Path,
        *,
        root: Path | None = None,
        maximum: int = MAX_WATCHES,
        max_bytes: int = MAX_WATCH_BYTES,
    ):
        key = hashlib.sha256(os.fsencode(session_path.resolve())).hexdigest()
        requested = (root or user_data_path("telegram-mcp") / "watches" / key).absolute()
        self.root = requested.parent.resolve() / requested.name
        self.path = self.root / "watches.sqlite3"
        self.maximum, self.max_bytes = maximum, max_bytes
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    def _db(self) -> sqlite3.Connection:
        if self._closed:
            raise storage_error()
        if self._connection is not None:
            return self._connection
        db = None
        try:
            db = open_private_database(self.path)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ToolError("WATCH_INCOMPATIBLE", "Unknown subscription database version.")
            if version == 0:
                if db.execute("SELECT name FROM sqlite_master").fetchone():
                    raise storage_error()
                db.execute("PRAGMA auto_vacuum = FULL")
                db.execute("PRAGMA secure_delete = ON")
                with db:
                    db.execute(
                        "CREATE TABLE watches (binding TEXT NOT NULL, name TEXT NOT NULL, "
                        "payload TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(binding, name))"
                    )
                    db.execute("PRAGMA user_version = 1")
            db.execute("PRAGMA secure_delete = ON")
            self._connection = db
            return db
        except (OSError, sqlite3.Error):
            if db is not None:
                db.close()
            raise storage_error() from None
        except ToolError:
            if db is not None:
                db.close()
            raise

    def get(self, name: str, binding: str) -> WatchState | None:
        try:
            row = (
                self._db()
                .execute(
                    "SELECT payload FROM watches WHERE binding = ? AND name = ?", (binding, name)
                )
                .fetchone()
            )
            return WatchState.model_validate_json(row[0]) if row else None
        except ValidationError:
            raise ToolError(
                "WATCH_INCOMPATIBLE", "Unsupported or damaged subscription state."
            ) from None
        except sqlite3.Error:
            raise storage_error() from None

    def names(self, binding: str, prefix: str, limit: int) -> list[str]:
        if self._connection is None and not self.path.exists():
            return []  # Completion must not create a missing store.
        try:
            return [
                row[0]
                for row in self._db().execute(
                    "SELECT name FROM watches WHERE binding = ? AND substr(name, 1, ?) = ? "
                    "ORDER BY name LIMIT ?",
                    (binding, len(prefix), prefix, limit),
                )
            ]
        except sqlite3.Error:
            raise storage_error() from None

    def list(self, binding: str) -> list[WatchState]:
        try:
            return [
                WatchState.model_validate_json(row[0])
                for row in self._db().execute(
                    "SELECT payload FROM watches WHERE binding = ? ORDER BY name", (binding,)
                )
            ]
        except ValidationError:
            raise ToolError(
                "WATCH_INCOMPATIBLE", "Unsupported or damaged subscription state."
            ) from None
        except sqlite3.Error:
            raise storage_error() from None

    def save(self, state: WatchState, binding: str) -> None:
        try:
            payload = WatchState.model_validate(state.model_dump()).model_dump_json()
            size = len(payload.encode("utf-8"))
            db = self._db()
            with db:
                count, used = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM watches "
                    "WHERE NOT(binding = ? AND name = ?)",
                    (binding, state.name),
                ).fetchone()
                if count >= self.maximum or used + size > self.max_bytes:
                    raise ToolError(
                        "WATCH_STORAGE_FULL",
                        "Subscription quota reached; no position or pending batch was discarded.",
                    )
                db.execute(
                    "INSERT INTO watches VALUES (?, ?, ?, ?) ON CONFLICT(binding, name) "
                    "DO UPDATE SET payload = excluded.payload, size = excluded.size",
                    (binding, state.name, payload, size),
                )
        except (sqlite3.Error, ValidationError):
            raise storage_error() from None

    def delete(self, name: str, binding: str) -> bool:
        try:
            db = self._db()
            with db:
                return bool(
                    db.execute(
                        "DELETE FROM watches WHERE binding = ? AND name = ?", (binding, name)
                    ).rowcount
                )
        except sqlite3.Error:
            raise storage_error() from None

    def close(self) -> None:
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            self._connection = None
