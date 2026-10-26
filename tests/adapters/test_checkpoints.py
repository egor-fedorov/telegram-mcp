import os
import sqlite3
from datetime import timedelta

import pytest

from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.domain.changes import CheckpointState, SeenMessage
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import FakeClock


def state(position=3):
    return CheckpointState(
        kind="changes",
        chat_id="1",
        phase="idle",
        position=position,
        upper_id=position,
        seen={3: SeenMessage(revision="a" * 64)},
    )


def test_checkpoints_survive_restart_and_are_immutable(tmp_path):
    clock = FakeClock()
    store = CheckpointStore(tmp_path / "account.session", clock, root=tmp_path / "state")
    original = state()
    saved = store.put(original, "auth")
    original.position = 10
    original.upper_id = 10
    store.put(original, "auth")
    store.defer("session", clock.utc_now() + timedelta(seconds=30))
    store.close()
    reopened = CheckpointStore(tmp_path / "account.session", clock, root=tmp_path / "state")
    try:
        assert reopened.get(saved.token, "auth", "changes").position == 3
        assert reopened.get(saved.token, "auth", "changes").seen[3].revision == "a" * 64
        assert reopened.retry_at("session") == clock.utc_now() + timedelta(seconds=30)
        first = reopened.get(saved.token, "auth", "changes")
        first.seen.clear()
        assert len(reopened.get(saved.token, "auth", "changes").seen) == 1
    finally:
        reopened.close()


def test_expiry_eviction_and_wrong_context_are_explicit(tmp_path):
    clock = FakeClock()
    store = CheckpointStore(tmp_path / "session", clock, root=tmp_path / "state", maximum=2)
    try:
        first = store.put(state(), "auth")
        second = store.put(state(4), "auth", retain=first.token)
        with pytest.raises(ToolError, match="CHECKPOINT_MISMATCH"):
            store.get(second.token, "another", "changes")
        with pytest.raises(ToolError, match="CHECKPOINT_MISMATCH"):
            store.get(second.token, "auth", "history")
        third = store.put(state(5), "auth", retain=second.token)
        with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
            store.get(first.token, "auth", "changes")
        assert store.get(second.token, "auth", "changes").position == 4
        clock.now = third.expires_at
        with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
            store.get(third.token, "auth", "changes")
    finally:
        store.close()


def test_payload_byte_budget_and_invalid_tokens(tmp_path):
    clock = FakeClock()
    size = len(state().model_dump_json().encode())
    store = CheckpointStore(tmp_path / "session", clock, root=tmp_path / "state", max_bytes=size)
    try:
        first = store.put(state(), "auth")
        second = store.put(state(), "auth", retain=first.token)
        assert store.get(second.token, "auth", "changes") == state()
        assert second.token == first.token
        with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_FULL"):
            store.put(state(4), "auth")
        for token in ("../path", "", "a" * 2000):
            with pytest.raises(ToolError, match="INVALID_CHECKPOINT"):
                store.get(token, "auth", "changes")
    finally:
        store.close()


@pytest.mark.parametrize(
    "damage",
    ["database_version", "state_version", "invalid_json", "garbage", "wrong_phase", "bad_offset"],
)
def test_corrupt_or_future_storage_is_not_silently_reset(tmp_path, damage):
    clock = FakeClock()
    store = CheckpointStore(tmp_path / "session", clock, root=tmp_path / "state")
    saved = store.put(state(), "auth")
    store.close()
    if damage == "garbage":
        store.path.write_bytes(b"not a database")
    else:
        with sqlite3.connect(store.path) as db:
            if damage == "database_version":
                db.execute("PRAGMA user_version = 99")
            elif damage == "state_version":
                db.execute(
                    "UPDATE checkpoints SET payload = ?",
                    (
                        state()
                        .model_dump_json()
                        .replace('"schema_version":1', '"schema_version":2'),
                    ),
                )
            elif damage == "wrong_phase":
                db.execute(
                    "UPDATE checkpoints SET payload = ?",
                    (state().model_dump_json().replace('"phase":"idle"', '"phase":"history"'),),
                )
            elif damage == "bad_offset":
                db.execute(
                    "UPDATE checkpoints SET payload = ?",
                    (
                        state()
                        .model_dump_json()
                        .replace('"recheck_index":0', '"recheck_index":100'),
                    ),
                )
            else:
                db.execute("UPDATE checkpoints SET payload = ?", ("{broken",))
    original_bytes = store.path.read_bytes()
    reopened = CheckpointStore(tmp_path / "session", clock, root=tmp_path / "state")
    try:
        with pytest.raises(ToolError, match="CHECKPOINT_(INCOMPATIBLE|STORAGE_ERROR)"):
            reopened.get(saved.token, "auth", "changes")
        assert store.path.read_bytes() == original_bytes
    finally:
        reopened.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode assertion")
def test_private_modes_and_redirected_database_are_rejected(tmp_path):
    store = CheckpointStore(tmp_path / "session", FakeClock(), root=tmp_path / "state")
    store.put(state(), "auth")
    store.close()
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    original = store.path.read_bytes()
    store.path.rename(tmp_path / "target")
    store.path.symlink_to(tmp_path / "target")
    reopened = CheckpointStore(tmp_path / "session", FakeClock(), root=tmp_path / "state")
    with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_ERROR"):
        reopened.put(state(), "auth")
    assert (tmp_path / "target").read_bytes() == original
    reopened.close()


def test_cooldown_only_moves_forward_and_closed_store_stays_closed(tmp_path):
    clock = FakeClock()
    store = CheckpointStore(tmp_path / "session", clock, root=tmp_path / "state")
    later = clock.now + timedelta(seconds=100)
    store.defer("session", later)
    store.defer("session", clock.now + timedelta(seconds=3))
    assert store.retry_at("session") == later
    store.close()
    with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_ERROR"):
        store.put(state(), "auth")


def test_failed_capacity_reservation_keeps_the_retained_input(tmp_path):
    store = CheckpointStore(tmp_path / "session", FakeClock(), root=tmp_path / "state", maximum=1)
    try:
        saved = store.put(state(), "auth")
        with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_FULL"):
            store.put(state(4), "auth", retain=saved.token)
        assert store.get(saved.token, "auth", "changes").position == 3
    finally:
        store.close()


def test_busy_sources_cannot_evict_a_quiet_source_after_4096_polls(tmp_path):
    clock = FakeClock()
    store = CheckpointStore(tmp_path / "session", clock, root=tmp_path / "state")
    try:
        # Exercise real quota transactions without benchmarking thousands of disk flushes.
        # Restart and publication tests keep the production durability settings.
        store._db().execute("PRAGMA synchronous = OFF")
        quiet = store.put(state().model_copy(update={"chat_id": "99"}), "auth")
        heads = {
            str(cid): store.put(state().model_copy(update={"chat_id": str(cid)}), "auth")
            for cid in range(1, 21)
        }
        for generation in range(210):
            clock.now += timedelta(minutes=10)
            for cid, head in list(heads.items()):
                heads[cid] = store.put(
                    state(generation + 4).model_copy(update={"chat_id": cid}),
                    "auth",
                    retain=head.token,
                )
        assert store.get(quiet.token, "auth", "changes").position == 3
        assert all(
            store.get(head.token, "auth", "changes").position == 213 for head in heads.values()
        )
        assert len(store._db().execute("SELECT token FROM checkpoints").fetchall()) == 4096
    finally:
        store.close()


def test_replay_forks_do_not_retire_another_consumers_head(tmp_path):
    store = CheckpointStore(tmp_path / "session", FakeClock(), root=tmp_path / "state", maximum=4)
    try:
        start = store.put(state(), "auth")
        independent = store.put(state(4), "auth", retain=start.token)
        branch = store.put(state(5), "auth", retain=start.token)
        for mid in range(6, 20):
            branch = store.put(state(mid), "auth", retain=branch.token)
        assert store.get(independent.token, "auth", "changes").position == 4
        assert store.get(branch.token, "auth", "changes").position == 19
    finally:
        store.close()


def test_unchanged_head_is_reused_and_renewed(tmp_path):
    clock = FakeClock()
    store = CheckpointStore(tmp_path / "session", clock, root=tmp_path / "state", maximum=1)
    try:
        first = store.put(state(), "auth")
        clock.now += timedelta(days=29)
        current = store.put(
            state().model_copy(update={"date_to": clock.now}), "auth", retain=first.token
        )
        assert current.token == first.token and current.expires_at > first.expires_at
        clock.now = first.expires_at
        assert store.get(current.token, "auth", "changes").position == 3
    finally:
        store.close()


def test_unpublished_file_receipt_keeps_parent_until_confirm_and_cleans_up_after_crash(tmp_path):
    store = CheckpointStore(tmp_path / "session", FakeClock(), root=tmp_path / "state", maximum=2)
    first = store.put(state(), "auth")
    unpublished = store.put(state(4), "auth", retain=first.token, pending=True)
    with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_FULL"):
        store.put(state(5), "auth")
    store.close()
    reopened = CheckpointStore(
        tmp_path / "session", FakeClock(), root=tmp_path / "state", maximum=2
    )
    try:
        assert reopened.get(first.token, "auth", "changes").position == 3
        with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
            reopened.get(unpublished.token, "auth", "changes")
        next_receipt = reopened.put(state(4), "auth", retain=first.token, pending=True)
        reopened.confirm(next_receipt.token)
        latest = reopened.put(state(5), "auth", retain=next_receipt.token)
        assert reopened.get(latest.token, "auth", "changes").position == 5
    finally:
        reopened.close()


def test_cooldown_reader_cannot_remove_an_active_writers_unpublished_receipt(tmp_path):
    root = tmp_path / "state"
    writer = CheckpointStore(tmp_path / "session", FakeClock(), root=root)
    reader = CheckpointStore(tmp_path / "session", FakeClock(), root=root)
    try:
        parent = writer.put(state(), "auth")
        pending = writer.put(state(4), "auth", retain=parent.token, pending=True)
        # A competing process checks the cooldown before attempting the session lease.
        assert reader.retry_at("session") is None
        assert reader.get(parent.token, "auth", "changes").position == 3
        with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
            reader.get(pending.token, "auth", "changes")
        writer.confirm(pending.token)
        assert reader.get(pending.token, "auth", "changes").position == 4
    finally:
        reader.close()
        writer.close()


def test_v1_migration_preserves_tokens_and_protects_latest_per_source(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    clock = FakeClock()
    with sqlite3.connect(root / "checkpoints.sqlite3") as db:
        db.execute(
            "CREATE TABLE checkpoints (token TEXT PRIMARY KEY, binding TEXT NOT NULL, "
            "kind TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL, "
            "size INTEGER NOT NULL)"
        )
        db.execute("CREATE TABLE cooldowns (binding TEXT PRIMARY KEY, until REAL NOT NULL)")
        for token, snapshot in (
            ("a" * 43, state()),
            ("b" * 43, state(4)),
            ("c" * 43, state().model_copy(update={"chat_id": "2"})),
        ):
            payload = snapshot.model_dump_json()
            db.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token,
                    "auth",
                    "changes",
                    (clock.now + timedelta(days=30)).timestamp(),
                    payload,
                    len(payload),
                ),
            )
        db.execute("PRAGMA user_version = 1")
    store = CheckpointStore(tmp_path / "session", clock, root=root, maximum=3)
    try:
        assert store.get("a" * 43, "auth", "changes").position == 3
        new = store.put(state(5), "auth", retain="b" * 43)
        assert store.get("c" * 43, "auth", "changes").chat_id == "2"
        assert store.get(new.token, "auth", "changes").position == 5
        with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
            store.get("a" * 43, "auth", "changes")
    finally:
        store.close()
