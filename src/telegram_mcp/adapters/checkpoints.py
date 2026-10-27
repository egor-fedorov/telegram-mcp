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

from telegram_mcp.adapters.state_database import open_private_database
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
        self._writer_initialized = False

    def _db(self) -> sqlite3.Connection:  # noqa: C901
        if self._closed:
            raise _storage_error()
        if self._connection is not None:
            return self._connection
        connection = None
        try:
            connection = open_private_database(self.path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
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
            if version < 2:
                with connection:
                    connection.execute(
                        "ALTER TABLE checkpoints ADD COLUMN head INTEGER NOT NULL DEFAULT 0"
                    )
                    connection.execute("ALTER TABLE checkpoints ADD COLUMN parent TEXT")
                    connection.execute(
                        "ALTER TABLE checkpoints ADD COLUMN published INTEGER NOT NULL DEFAULT 1"
                    )
                    # Legacy snapshots have no ancestry. Protect the newest valid
                    # snapshot of each source; older tokens remain readable.
                    latest: dict[tuple[str, str, str], str] = {}
                    for token, binding, kind, payload in connection.execute(
                        "SELECT token, binding, kind, payload FROM checkpoints ORDER BY rowid"
                    ):
                        try:
                            state = CheckpointState.model_validate_json(payload)
                        except ValidationError:
                            continue
                        latest[binding, kind, state.chat_id] = token
                    connection.executemany(
                        "UPDATE checkpoints SET head = 1 WHERE token = ?",
                        ((token,) for token in latest.values()),
                    )
                    connection.execute("CREATE INDEX checkpoint_parents ON checkpoints(parent)")
                    connection.execute("PRAGMA user_version = 2")
            if version < 3:
                with connection:
                    # Legacy roots may have been continued without changing their
                    # token. Never guess that such a consumer was abandoned.
                    connection.execute(
                        "ALTER TABLE checkpoints ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0"
                    )
                    connection.execute("ALTER TABLE checkpoints ADD COLUMN chat_id TEXT")
                    connection.execute("ALTER TABLE checkpoints ADD COLUMN replacement TEXT")
                    for token, payload in connection.execute(
                        "SELECT token, payload FROM checkpoints"
                    ).fetchall():
                        try:
                            source = CheckpointState.model_validate_json(payload).chat_id
                        except ValidationError:
                            continue
                        connection.execute(
                            "UPDATE checkpoints SET chat_id = ? WHERE token = ?", (source, token)
                        )
                    connection.execute(
                        "CREATE INDEX checkpoint_baselines "
                        "ON checkpoints(binding, chat_id, baseline)"
                    )
                    connection.execute("PRAGMA user_version = 3")
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
                    "SELECT binding, kind, expires, payload FROM checkpoints "
                    "WHERE token = ? AND published = 1",
                    (token,),
                )
                .fetchone()
            )
            if row is None or row[2] <= self.clock.utc_now().timestamp():
                raise ToolError(
                    "CHECKPOINT_EXPIRED",
                    "Checkpoint expired or an old version was evicted. Recover the missing "
                    "period with get_history/export_history; a new baseline skips old messages.",
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

    def put(  # noqa: C901
        self,
        state: CheckpointState,
        binding: str,
        *,
        retain: str | None = None,
        pending: bool = False,
    ) -> CheckpointReceipt:
        try:
            payload = CheckpointState.model_validate(state.model_dump()).model_dump_json()
        except ValidationError:
            raise _storage_error() from None
        size = len(payload.encode("utf-8"))
        if size > self.max_bytes:
            raise ToolError("CHECKPOINT_STORAGE_FULL", "Checkpoint exceeds the storage budget.")
        now = self.clock.utc_now()
        expires = now + timedelta(seconds=CHECKPOINT_TTL_SECONDS)
        token = secrets.token_urlsafe(32)
        try:
            db = self._db()
            if not self._writer_initialized:
                # A write follows authenticated source preparation and ownership
                # of the Telegram session lease. Merely reading a cooldown before
                # acquiring that lease must not remove another process's receipt.
                with db:
                    db.execute("DELETE FROM checkpoints WHERE published = 0")
                self._writer_initialized = True
            with db:
                db.execute("DELETE FROM checkpoints WHERE expires <= ?", (now.timestamp(),))
                db.execute("DELETE FROM cooldowns WHERE until <= ?", (now.timestamp(),))
                replacement = None
                previous = None
                candidate = retain
                if retain is None and state.kind == "changes":
                    row = db.execute(
                        "SELECT token FROM checkpoints WHERE binding = ? AND chat_id = ? "
                        "AND kind = 'changes' AND baseline = 1 AND head = 1 AND published = 1 "
                        "AND token NOT IN (SELECT replacement FROM checkpoints "
                        "WHERE published = 0 AND replacement IS NOT NULL) "
                        "AND token NOT IN (SELECT parent FROM checkpoints "
                        "WHERE published = 0 AND parent IS NOT NULL) ORDER BY rowid DESC LIMIT 1",
                        (binding, state.chat_id),
                    ).fetchone()
                    if row is not None:
                        candidate = replacement = row[0]
                if candidate is not None:
                    previous = self.get(candidate, binding, state.kind)
                    if previous.chat_id != state.chat_id:
                        raise ToolError("CHECKPOINT_MISMATCH", "Checkpoint source has changed.")
                if retain is not None:
                    # A caller has used this position. Keep it protected even if
                    # a later file publication fails and the caller must retry.
                    db.execute("UPDATE checkpoints SET baseline = 0 WHERE token = ?", (retain,))
                if candidate is not None and previous is not None:
                    is_head = db.execute(
                        "SELECT head FROM checkpoints WHERE token = ?", (candidate,)
                    ).fetchone()[0]
                    excluded = (
                        {"date_to"} if state.kind == "changes" and state.phase == "idle" else set()
                    )
                    if is_head and previous.model_dump(exclude=excluded) == state.model_dump(
                        exclude=excluded
                    ):
                        db.execute(
                            "UPDATE checkpoints SET expires = ? WHERE token = ?",
                            (expires.timestamp(), candidate),
                        )
                        return CheckpointReceipt(candidate, expires)
                count, used = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM checkpoints "
                    "WHERE token NOT IN (SELECT replacement FROM checkpoints "
                    "WHERE published = 0 AND replacement IS NOT NULL)"
                ).fetchone()
                # An unused root can reserve its replacement without destroying
                # the old token before files are published. Each reservation has
                # one exclusive victim; failed confirmation restores the old head.
                if replacement is not None:
                    count -= 1
                    used -= db.execute(
                        "SELECT size FROM checkpoints WHERE token = ?", (replacement,)
                    ).fetchone()[0]
                while count >= self.maximum or used + size > self.max_bytes:
                    oldest = db.execute(
                        "SELECT token, size FROM checkpoints WHERE token != ? AND head = 0 "
                        "AND token NOT IN (SELECT parent FROM checkpoints "
                        "WHERE published = 0 AND parent IS NOT NULL) "
                        "AND token NOT IN (SELECT replacement FROM checkpoints "
                        "WHERE published = 0 AND replacement IS NOT NULL) "
                        "ORDER BY rowid LIMIT 1",
                        (retain or "",),
                    ).fetchone()
                    if oldest is None:
                        raise ToolError(
                            "CHECKPOINT_STORAGE_FULL",
                            "Storage contains protected checkpoints; no position was discarded. "
                            "Inspect 'telegram-mcp checkpoints status' for local cleanup.",
                        )
                    db.execute("DELETE FROM checkpoints WHERE token = ?", (oldest[0],))
                    count, used = count - 1, used - oldest[1]
                db.execute(
                    "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (
                        token,
                        binding,
                        state.kind,
                        expires.timestamp(),
                        payload,
                        size,
                        retain,
                        int(not pending),
                        int(retain is None and state.kind == "changes"),
                        state.chat_id,
                        replacement,
                    ),
                )
                if not pending and retain is not None:
                    db.execute("UPDATE checkpoints SET head = 0 WHERE token = ?", (retain,))
                if not pending:
                    self._publish_baseline(db, token)
                    self._trim(db)
        except sqlite3.Error:
            raise _storage_error() from None
        return CheckpointReceipt(token, expires)

    def confirm(self, token: str) -> None:
        """Publish a receipt only after its associated files have been created."""
        try:
            db = self._db()
            with db:
                row = db.execute(
                    "SELECT parent, published FROM checkpoints WHERE token = ?", (token,)
                ).fetchone()
                if row is None:
                    raise _storage_error()
                if not row[1]:
                    db.execute("UPDATE checkpoints SET head = 0 WHERE token = ?", (row[0],))
                    db.execute("UPDATE checkpoints SET published = 1 WHERE token = ?", (token,))
                    self._publish_baseline(db, token)
                    self._trim(db)
        except sqlite3.Error:
            raise _storage_error() from None

    @staticmethod
    def _publish_baseline(db: sqlite3.Connection, token: str) -> None:
        row = db.execute(
            "SELECT binding, chat_id, baseline FROM checkpoints WHERE token = ?", (token,)
        ).fetchone()
        if row is not None and row[2]:
            db.execute(
                "UPDATE checkpoints SET head = 0 WHERE binding = ? AND chat_id = ? "
                "AND baseline = 1 AND published = 1 AND token != ?",
                (row[0], row[1], token),
            )
        db.execute("UPDATE checkpoints SET replacement = NULL WHERE token = ?", (token,))

    def _trim(self, db: sqlite3.Connection) -> None:
        # Other in-flight root replacements have their own exclusive reservations.
        count, used = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM checkpoints "
            "WHERE token NOT IN (SELECT replacement FROM checkpoints "
            "WHERE published = 0 AND replacement IS NOT NULL)"
        ).fetchone()
        while count > self.maximum or used > self.max_bytes:
            row = db.execute(
                "SELECT token, size FROM checkpoints WHERE head = 0 AND published = 1 "
                "AND token NOT IN (SELECT parent FROM checkpoints "
                "WHERE published = 0 AND parent IS NOT NULL) "
                "AND token NOT IN (SELECT replacement FROM checkpoints "
                "WHERE published = 0 AND replacement IS NOT NULL) ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                raise ToolError(
                    "CHECKPOINT_STORAGE_FULL",
                    "Storage contains protected checkpoints; "
                    "inspect 'telegram-mcp checkpoints status'.",
                )
            db.execute("DELETE FROM checkpoints WHERE token = ?", (row[0],))
            count, used = count - 1, used - row[1]

    def discard(self, token: str) -> None:
        """Discard an unpublished receipt without touching the caller's input."""
        try:
            db = self._db()
            with db:
                db.execute("DELETE FROM checkpoints WHERE token = ? AND published = 0", (token,))
        except sqlite3.Error:
            raise _storage_error() from None

    def status(self) -> dict[str, object]:
        """Local metadata only; callers must own the session lease for maintenance."""
        try:
            rows = (
                self._db()
                .execute(
                    "SELECT chat_id, kind, COUNT(*), SUM(size), SUM(head), SUM(baseline), "
                    "SUM(expires <= ?) FROM checkpoints "
                    "GROUP BY chat_id, kind ORDER BY chat_id, kind",
                    (self.clock.utc_now().timestamp(),),
                )
                .fetchall()
            )
            keys = ("chat_id", "kind", "count", "bytes", "protected", "unused_baselines", "expired")
            return {
                "count": sum(row[2] for row in rows),
                "bytes": sum(row[3] for row in rows),
                "maximum": self.maximum,
                "max_bytes": self.max_bytes,
                "sources": [dict(zip(keys, row, strict=True)) for row in rows],
            }
        except sqlite3.Error:
            raise _storage_error() from None

    def prune(
        self, *, chat_id: str | None = None, include_protected: bool = False, apply: bool = False
    ) -> dict[str, object]:
        """Preview or explicitly delete selected snapshots, never watches or cooldowns."""
        try:
            db = self._db()
            condition = (
                "(? IS NULL OR chat_id = ?) AND (? OR head = 0 OR expires <= ? OR published = 0)"
            )
            args = (chat_id, chat_id, include_protected, self.clock.utc_now().timestamp())
            with db:
                count, size = db.execute(
                    f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM checkpoints WHERE {condition}",
                    args,
                ).fetchone()
                if apply:
                    db.execute(f"DELETE FROM checkpoints WHERE {condition}", args)
            return {"applied": apply, "selected_count": count, "selected_bytes": size}
        except sqlite3.Error:
            raise _storage_error() from None

    def retry_at(self, binding: str) -> datetime | None:
        if self._closed:
            raise _storage_error()
        connection = self._connection
        owned = False
        try:
            if connection is None:
                if not self.path.exists():
                    return None
                # This read precedes ownership of the Telegram session. In
                # particular, do not migrate an old server's live schema here.
                connection = open_private_database(self.path)
                owned = True
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3):
                    raise ToolError(
                        "CHECKPOINT_INCOMPATIBLE", "Unknown checkpoint database version."
                    )
                if (
                    version == 0
                    and not connection.execute("SELECT name FROM sqlite_master").fetchone()
                ):
                    return None
            row = connection.execute(
                "SELECT until FROM cooldowns WHERE binding = ?", (binding,)
            ).fetchone()
            return datetime.fromtimestamp(row[0], UTC) if row else None
        except (OSError, sqlite3.Error, ValueError, OverflowError):
            raise _storage_error() from None
        finally:
            if owned and connection is not None:
                connection.close()

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
