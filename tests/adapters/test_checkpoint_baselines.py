import sqlite3
from datetime import timedelta

import pytest

from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.domain.changes import CheckpointState
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import FakeClock


def snapshot(mid=1, cid="1"):
    return CheckpointState(kind="changes", chat_id=cid, phase="idle", position=mid, upper_id=mid)


@pytest.fixture
def store(tmp_path):
    result = CheckpointStore(
        tmp_path / "session", FakeClock(), root=tmp_path / "checkpoints", maximum=4
    )
    try:
        yield result
    finally:
        result.close()


def test_thousands_of_fresh_baselines_preserve_only_latest_unused_root_and_quiet_chat(store):
    quiet = store.put(snapshot(cid="2"), "auth")
    for mid in range(1, 4201):
        current = store.put(snapshot(mid), "auth", pending=True)
        store.confirm(current.token)
    assert store.get(quiet.token, "auth", "changes").chat_id == "2"
    assert store.get(current.token, "auth", "changes").position == 4200
    status = store.status()
    assert status["count"] == 4
    assert store._db().execute("SELECT SUM(head) FROM checkpoints").fetchone()[0] == 2


def test_noop_baselines_reuse_token_but_continuation_protects_independent_consumer(store):
    first = store.put(snapshot(), "auth")
    assert store.put(snapshot(), "auth").token == first.token
    assert store.put(snapshot(), "auth", retain=first.token).token == first.token
    second = store.put(snapshot(), "auth")
    assert second.token != first.token
    for mid in range(2, 10):
        store.put(snapshot(mid), "auth")
    assert store.get(first.token, "auth", "changes").position == 1
    with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
        store.get(second.token, "auth", "changes")


def test_root_replacement_at_full_capacity_is_published_atomically(store):
    store.maximum = 1
    first = store.put(snapshot(), "auth")
    pending = store.put(snapshot(2), "auth", pending=True)
    assert store.get(first.token, "auth", "changes").position == 1
    store.discard(pending.token)
    assert store.get(first.token, "auth", "changes").position == 1
    pending = store.put(snapshot(2), "auth", pending=True)
    store.confirm(pending.token)
    assert store.status()["count"] == 1
    with pytest.raises(ToolError, match="CHECKPOINT_EXPIRED"):
        store.get(first.token, "auth", "changes")


def test_concurrent_continuation_prevents_pending_baseline_from_retiring_input(store):
    store.maximum = 1
    first = store.put(snapshot(), "auth")
    pending = store.put(snapshot(2), "auth", pending=True)
    assert store.put(snapshot(), "auth", retain=first.token).token == first.token
    with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_FULL"):
        store.confirm(pending.token)
    store.discard(pending.token)
    assert store.get(first.token, "auth", "changes").position == 1


def test_full_v2_database_migrates_conservatively_and_offline_prune_restores_capacity(tmp_path):
    root = tmp_path / "checkpoints"
    root.mkdir()
    clock = FakeClock()
    until = clock.now + timedelta(seconds=60)
    with sqlite3.connect(root / "checkpoints.sqlite3") as db:
        db.execute(
            "CREATE TABLE checkpoints (token TEXT PRIMARY KEY, binding TEXT, kind TEXT, "
            "expires REAL, payload TEXT, size INTEGER, head INTEGER, "
            "parent TEXT, published INTEGER)"
        )
        db.execute("CREATE TABLE cooldowns (binding TEXT PRIMARY KEY, until REAL)")
        db.execute("INSERT INTO cooldowns VALUES ('session', ?)", (until.timestamp(),))
        for mid in range(1, 5):
            payload = snapshot(mid).model_dump_json()
            db.execute(
                "INSERT INTO checkpoints VALUES (?, 'auth', 'changes', ?, ?, ?, 1, NULL, 1)",
                (
                    str(mid) * 43,
                    (clock.now + timedelta(days=30)).timestamp(),
                    payload,
                    len(payload),
                ),
            )
        db.execute("PRAGMA user_version = 2")
    store = CheckpointStore(tmp_path / "session", clock, root=root, maximum=4)
    try:
        assert store.status()["count"] == 4
        with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_FULL"):
            store.put(snapshot(5), "auth")
        assert store.prune()["selected_count"] == 0
        assert store.prune(chat_id="1", include_protected=True)["selected_count"] == 4
        assert store.status()["count"] == 4
        store.prune(chat_id="1", include_protected=True, apply=True)
        assert store.status()["count"] == 0
        assert store.retry_at("session") == until
        assert store.get(store.put(snapshot(5), "auth").token, "auth", "changes").position == 5
    finally:
        store.close()
