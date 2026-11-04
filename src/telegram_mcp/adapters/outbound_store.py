"""Private, account-bound files and a non-evicting outbound idempotency journal."""

import asyncio
import hashlib
import mimetypes
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from telegram_mcp.adapters.local_uploads import read_allowed_file
from telegram_mcp.adapters.state_database import open_private_database
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.outbound import Draft, OutboundOperation, RegisteredFile

MAX_FILE = 20 * 1024 * 1024
MAX_LIBRARY = 100 * 1024 * 1024
MAX_FILES = 32
MAX_OPERATIONS = 100_000


class OutboundStore:
    def __init__(self, session: Path, directories: tuple[Path, ...] = ()):
        self.path = session.with_suffix(".outbound.sqlite3")
        self.directories = directories
        self._lock = asyncio.Lock()
        self._closed = False

    def _open(self) -> sqlite3.Connection:
        db = open_private_database(self.path)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, metadata TEXT NOT NULL,
                data BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, key TEXT NOT NULL,
                value TEXT NOT NULL, UNIQUE(account, key));
            CREATE TABLE IF NOT EXISTS drafts (
                account TEXT NOT NULL, chat TEXT NOT NULL, value TEXT NOT NULL,
                previous TEXT NOT NULL, PRIMARY KEY(account, chat));
            CREATE TABLE IF NOT EXISTS cooldowns (
                account TEXT NOT NULL, scope TEXT NOT NULL, until TEXT NOT NULL,
                PRIMARY KEY(account, scope));
        """)
        return db

    async def _run[T](self, action: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            if self._closed:
                raise ToolError("OUTBOUND_STORAGE_UNAVAILABLE", "Outbound storage is closed.")

            def work() -> T:
                try:
                    db = self._open()
                    try:
                        with db:
                            return action(db)
                    finally:
                        db.close()
                except (sqlite3.Error, OSError):
                    raise ToolError(
                        "OUTBOUND_STORAGE_UNAVAILABLE", "Cannot access private outbound storage."
                    ) from None

            # Shield and join cancellation: no background commit can race the next call.
            task = asyncio.create_task(asyncio.to_thread(work))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def register(
        self,
        account: str,
        name: str,
        mime: str,
        data: bytes,
        now: datetime,
        attachment_id: str | None = None,
        source_identity: str | None = None,
    ) -> RegisteredFile:
        if not data or len(data) > MAX_FILE:
            raise ToolError("FILE_TOO_LARGE", "Use a nonempty file no larger than 20 MiB.")

        def write(db: sqlite3.Connection) -> RegisteredFile:
            count, size = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(data)), 0) FROM files"
            ).fetchone()
            if count >= MAX_FILES or size + len(data) > MAX_LIBRARY:
                raise ToolError(
                    "FILE_STORAGE_FULL", "Remove unused files with the local files CLI."
                )
            file = RegisteredFile(
                file_id=secrets.token_urlsafe(32),
                name=Path(name.replace("\\", "/")).name[:255],
                mime_type=mime,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                created_at=now,
                attachment_id=attachment_id,
                source_identity=source_identity,
            )
            db.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?)",
                (file.file_id, account, file.model_dump_json(), data),
            )
            return file

        return await self._run(write)

    async def import_local(self, account: str, path: str, now: datetime) -> RegisteredFile:
        data = await asyncio.to_thread(read_allowed_file, Path(path), self.directories, MAX_FILE)
        return await self.register(
            account,
            Path(path).name,
            mimetypes.guess_type(path)[0] or "application/octet-stream",
            data,
            now,
        )

    async def files(self, account: str) -> list[RegisteredFile]:
        return await self._run(
            lambda db: [
                RegisteredFile.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT metadata FROM files WHERE account=? ORDER BY id", (account,)
                )
            ]
        )

    async def all_files(self) -> list[RegisteredFile]:
        return await self._run(
            lambda db: [
                RegisteredFile.model_validate_json(row[0])
                for row in db.execute("SELECT metadata FROM files ORDER BY id")
            ]
        )

    async def file(self, account: str, file_id: str) -> tuple[RegisteredFile, bytes]:
        def read(db: sqlite3.Connection) -> tuple[RegisteredFile, bytes]:
            row = db.execute(
                "SELECT metadata, data FROM files WHERE account=? AND id=?", (account, file_id)
            ).fetchone()
            if row is None:
                raise ToolError("FILE_UNAVAILABLE", "Unknown registered file for this account.")
            metadata = RegisteredFile.model_validate_json(row[0])
            if (
                len(row[1]) != metadata.size
                or hashlib.sha256(row[1]).hexdigest() != metadata.sha256
            ):
                raise ToolError("FILE_UNAVAILABLE", "Registered file integrity check failed.")
            return metadata, row[1]

        return await self._run(read)

    async def operation(self, account: str, key: str) -> OutboundOperation | None:
        def read(db: sqlite3.Connection) -> OutboundOperation | None:
            row = db.execute(
                "SELECT value FROM operations WHERE account=? AND key=?", (account, key)
            ).fetchone()
            return OutboundOperation.model_validate_json(row[0]) if row else None

        return await self._run(read)

    async def lookup_operation(self, account: str, operation_id: str) -> OutboundOperation:
        def read(db: sqlite3.Connection) -> OutboundOperation:
            row = db.execute(
                "SELECT value FROM operations WHERE account=? AND id=?", (account, operation_id)
            ).fetchone()
            if row is None:
                raise ToolError("OPERATION_UNAVAILABLE", "Unknown operation for this account.")
            return OutboundOperation.model_validate_json(row[0])

        return await self._run(read)

    async def create_operation(self, operation: OutboundOperation) -> OutboundOperation:
        def write(db: sqlite3.Connection) -> OutboundOperation:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM operations WHERE account=? AND key=?",
                (operation.account_id, operation.idempotency_key),
            ).fetchone()
            if row:
                return OutboundOperation.model_validate_json(row[0])
            count, size = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(value)), 0) FROM operations"
            ).fetchone()
            if count >= MAX_OPERATIONS or size >= 128 * 1024 * 1024:
                raise ToolError(
                    "OPERATION_STORAGE_FULL",
                    "Journal capacity reached; existing operations remain recoverable.",
                )
            db.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?)",
                (
                    operation.operation_id,
                    operation.account_id,
                    operation.idempotency_key,
                    operation.model_dump_json(),
                ),
            )
            return operation

        return await self._run(write)

    async def save_operation(self, operation: OutboundOperation) -> None:
        await self._run(
            lambda db: db.execute(
                "UPDATE operations SET value=? WHERE id=? AND account=?",
                (operation.model_dump_json(), operation.operation_id, operation.account_id),
            )
        )

    async def owned_draft(self, account: str, chat: str) -> Draft | None:
        def read(db: sqlite3.Connection) -> Draft | None:
            row = db.execute(
                "SELECT value FROM drafts WHERE account=? AND chat=?", (account, chat)
            ).fetchone()
            return Draft.model_validate_json(row[0]) if row else None

        return await self._run(read)

    async def save_draft(self, account: str, draft: Draft, previous: Draft) -> None:
        await self._run(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO drafts VALUES (?, ?, ?, ?)",
                (account, draft.chat_id, draft.model_dump_json(), previous.model_dump_json()),
            )
        )

    async def remove_file(self, file_id: str) -> None:
        await self._run(lambda db: db.execute("DELETE FROM files WHERE id=?", (file_id,)))

    async def journal(self) -> list[dict]:
        def read(db: sqlite3.Connection) -> list[dict]:
            return [
                {
                    "operation_id": op.operation_id,
                    "kind": op.kind,
                    "status": op.receipt.status,
                    "chat_id": op.chat_id,
                }
                for row in db.execute("SELECT value FROM operations")
                if (op := OutboundOperation.model_validate_json(row[0]))
            ]

        return await self._run(read)

    async def prune_payloads(self) -> int:
        def write(db: sqlite3.Connection) -> int:
            count = 0
            for row in db.execute("SELECT value FROM operations").fetchall():
                op = OutboundOperation.model_validate_json(row[0])
                if op.payload and op.receipt.status in ("sent", "completed", "failed"):
                    op.payload = {}
                    db.execute(
                        "UPDATE operations SET value=? WHERE id=?",
                        (op.model_dump_json(), op.operation_id),
                    )
                    count += 1
            return count

        return await self._run(write)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True

    async def cooldowns(self, account: str, now: datetime) -> dict[str, datetime]:
        return await self._run(
            lambda db: {
                scope: until
                for scope, value in db.execute(
                    "SELECT scope, until FROM cooldowns WHERE account=?", (account,)
                )
                if (until := datetime.fromisoformat(value)) > now
            }
        )

    async def set_cooldown(self, account: str, scope: str, until: datetime) -> None:
        await self._run(
            lambda db: db.execute(
                "INSERT INTO cooldowns VALUES (?, ?, ?) ON CONFLICT(account, scope) "
                "DO UPDATE SET until=MAX(until, excluded.until)",
                (account, scope, until.isoformat()),
            )
        )
