"""Old journal bytes and deduplication identities survive indexed query migration."""

import json
import sqlite3

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
