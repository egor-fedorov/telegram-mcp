"""Old journal bytes and deduplication identities survive indexed query migration."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from telegram_mcp.adapters.outbound_store import OutboundStore
from telegram_mcp.application.outbound_ports import OperationFilter
from telegram_mcp.domain.outbound import OutboundOperation, SendReceipt
from tests.support.fakes import BASE


async def test_legacy_journal_migration_preserves_values_and_unknown_dates(tmp_path):
    session = tmp_path / "account.session"
    path = session.with_suffix(".outbound.sqlite3")
    legacy = {
        "operation_id": "original",
        "account_id": "account",
        "idempotency_key": "original-key",
        "kind": "message",
        "request_hash": "unchanged-hash",
        "payload": {},
        "chat_id": "1",
        "random_id": 12345678,
        "confirmed": True,
        "dispatched": True,
        "receipt": {"operation_id": "original", "status": "sent", "chat_id": "1"},
    }
    value = json.dumps(legacy)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE operations (id TEXT PRIMARY KEY, account TEXT NOT NULL, "
            "key TEXT NOT NULL, value TEXT NOT NULL, UNIQUE(account, key))"
        )
        db.execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?)",
            ("original", "account", "original-key", value),
        )
    store = OutboundStore(session)
    try:
        batch = await store.operations(
            "account", OperationFilter(), before=None, limit=1, active_ids=()
        )
        original = batch.operations[0]
        assert original.created_at is None and original.updated_at is None
        assert original.random_id == 12345678 and original.request_hash == "unchanged-hash"
        assert original.expected_text_sha256 is None
        assert not (
            await store.operations(
                "account", OperationFilter(date_from=BASE), before=None, limit=1, active_ids=()
            )
        ).operations
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT value FROM operations").fetchone()[0] == value
            assert len(db.execute("PRAGMA index_list(operations)").fetchall()) >= 6
        duplicate = OutboundOperation.model_validate(legacy)
        duplicate.operation_id = "different"
        assert (await store.create_operation(duplicate)).operation_id == "original"
        new = duplicate.model_copy(
            update={"operation_id": "new", "idempotency_key": "new-key", "created_at": BASE}
        )
        new.receipt = SendReceipt(operation_id="new", status="failed", chat_id="1")
        await store.create_operation(new)
        filtered = await store.operations(
            "account",
            OperationFilter(status="failed", date_from=BASE),
            before=None,
            limit=1,
            active_ids=(),
        )
        assert [op.operation_id for op in filtered.operations] == ["new"]
        assert not (
            await store.operations("other", OperationFilter(), before=None, limit=1, active_ids=())
        ).operations
    finally:
        await store.close()


async def test_truncated_legacy_date_index_is_rebuilt_without_changing_receipts(tmp_path):
    session = tmp_path / "account.session"
    path = session.with_suffix(".outbound.sqlite3")
    date = datetime(2026, 11, 5, 15, 32, 16, 565436, tzinfo=UTC)
    operation = OutboundOperation(
        operation_id="original",
        account_id="account",
        idempotency_key="key",
        kind="message",
        request_hash="preserved",
        payload={},
        chat_id="1",
        random_id=123,
        created_at=date,
        receipt=SendReceipt(operation_id="original", status="pending", chat_id="1"),
    )
    original = operation.model_dump_json()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE operations (id TEXT PRIMARY KEY, account TEXT, key TEXT, value TEXT, "
            "chat_id TEXT, kind TEXT, status TEXT, created_at REAL, dispatched INTEGER, "
            "UNIQUE(account, key))"
        )
        db.execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "original",
                "account",
                "key",
                original,
                "1",
                "message",
                "pending",
                date.timestamp(),
                0,
            ),
        )
        db.execute("CREATE INDEX operations_date ON operations(account, created_at)")
    content = path.read_bytes()
    assert content.endswith(b"\x1a")
    path.write_bytes(content[:-1])  # Reproduce the Windows CRT's text-mode truncation.
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
    store = OutboundStore(session)
    try:
        batch = await store.operations(
            "account", OperationFilter(date_from=date), before=None, limit=10, active_ids=()
        )
        assert batch.operations == [operation]
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.execute("SELECT value FROM operations").fetchone()[0] == original
        operation.receipt.status = "failed"
        await store.save_operation(operation)
        assert await store.operation("account", "key") == operation
    finally:
        await store.close()
