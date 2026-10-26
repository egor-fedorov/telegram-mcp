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
        first, second = store.put(state(), "auth"), store.put(state(4), "auth")
        with pytest.raises(ToolError, match="CHECKPOINT_MISMATCH"):
            store.get(second.token, "another", "changes")
        with pytest.raises(ToolError, match="CHECKPOINT_MISMATCH"):
            store.get(second.token, "auth", "history")
        third = store.put(state(5), "auth")
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
        second = store.put(state(), "auth")
        assert store.get(second.token, "auth", "changes") == state()
        with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
            store.get(first.token, "auth", "changes")
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
                db.execute("PRAGMA user_version = 2")
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
        with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_ERROR"):
            store.put(state(4), "auth", retain=saved.token)
        assert store.get(saved.token, "auth", "changes").position == 3
    finally:
        store.close()
