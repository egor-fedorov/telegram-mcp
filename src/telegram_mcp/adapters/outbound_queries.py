"""Indexed journal queries and an additive migration that preserves original records."""

import sqlite3

from telegram_mcp.application.outbound_ports import OperationBatch, OperationFilter
from telegram_mcp.domain.outbound import OutboundOperation


def index_values(operation: OutboundOperation) -> tuple:
    return (
        operation.chat_id,
        operation.kind,
        operation.receipt.status,
        operation.created_at.timestamp() if operation.created_at else None,
        int(operation.dispatched),
    )


def prepare_indexes(db: sqlite3.Connection) -> None:
    if "chat_id" not in {row[1] for row in db.execute("PRAGMA table_info(operations)")}:
        with db:
            db.execute("BEGIN IMMEDIATE")
            for name, kind in (
                ("chat_id", "TEXT"),
                ("kind", "TEXT"),
                ("status", "TEXT"),
                ("created_at", "REAL"),
                ("dispatched", "INTEGER"),
            ):
                db.execute(f"ALTER TABLE operations ADD COLUMN {name} {kind}")
            rows = db.execute("SELECT id, value FROM operations")
            while batch := rows.fetchmany(256):
                for ident, value in batch:
                    operation = OutboundOperation.model_validate_json(value)
                    db.execute(
                        "UPDATE operations SET chat_id=?, kind=?, status=?, created_at=?, "
                        "dispatched=? WHERE id=?",
                        (*index_values(operation), ident),
                    )
    db.executescript("""
        -- Replace the derived index that a Windows text-mode open could truncate.
        -- The authoritative table and JSON are left intact.
        DROP INDEX IF EXISTS operations_date;
        CREATE INDEX IF NOT EXISTS operations_account ON operations(account);
        CREATE INDEX IF NOT EXISTS operations_chat ON operations(account, chat_id);
        CREATE INDEX IF NOT EXISTS operations_status ON operations(account, status);
        CREATE INDEX IF NOT EXISTS operations_created_at ON operations(account, created_at);
    """)


def query_operations(
    db: sqlite3.Connection,
    account: str,
    filters: OperationFilter,
    before: int | None,
    limit: int,
    active_ids: tuple[str, ...],
) -> OperationBatch:
    clauses = ["account=?"]
    params: list[str | int | float] = [account]
    if before is not None:
        clauses.append("rowid < ?")
        params.append(before)
    for name, value in (("chat_id", filters.chat_id), ("kind", filters.kind)):
        if value is not None:
            clauses.append(f"{name}=?")
            params.append(value)
    for comparison, date in ((">=", filters.date_from), ("<", filters.date_to)):
        if date is not None:
            clauses.append(f"created_at {comparison} ?")
            params.append(date.timestamp())
    if filters.status is not None:
        raw_states = ("pending", "unknown") if filters.status == "unknown" else (filters.status,)
        clauses.append(f"status IN ({','.join('?' for _ in raw_states)})")
        params.extend(raw_states)
        # A dispatched pending record left by a stopped process is uncertain, never pending.
        active = ",".join("?" for _ in active_ids) or "NULL"
        inactive = f"id NOT IN ({active})" if active_ids else "1"
        clauses.append(
            f"CASE WHEN status='pending' AND dispatched=1 AND {inactive} "
            "THEN 'unknown' ELSE status END = ?"
        )
        params.extend(active_ids)
        params.append(filters.status)
    rows = db.execute(
        f"SELECT rowid, value FROM operations WHERE {' AND '.join(clauses)} "
        "ORDER BY rowid DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    page = rows[:limit]
    return OperationBatch(
        [OutboundOperation.model_validate_json(value) for _, value in page],
        page[-1][0] if len(rows) > limit else None,
    )
