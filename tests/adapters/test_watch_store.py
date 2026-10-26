import os
import sqlite3

import pytest

from telegram_mcp.adapters.watches import WatchStore
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.watches import WatchState
from tests.support.fakes import BASE


def test_quota_never_evicts_names_and_deletion_releases_capacity(tmp_path):
    store = WatchStore(tmp_path / "session", root=tmp_path / "state", maximum=1)
    first = WatchState(name="first", chat_ids=["1"], started_at=BASE)
    try:
        store.save(first, "auth")
        with pytest.raises(ToolError, match="WATCH_STORAGE_FULL"):
            store.save(first.model_copy(update={"name": "second"}), "auth")
        assert store.get("first", "auth") == first
        assert store.get("first", "other") is None
        assert store.delete("first", "auth")
        store.save(first.model_copy(update={"name": "second"}), "auth")
    finally:
        store.close()
    reopened = WatchStore(tmp_path / "session", root=tmp_path / "state")
    try:
        assert [state.name for state in reopened.list("auth")] == ["second"]
    finally:
        reopened.close()


@pytest.mark.parametrize("damage", ["version", "payload"])
def test_watch_corruption_is_reported_without_reset(tmp_path, damage):
    store = WatchStore(tmp_path / "session", root=tmp_path / "state")
    store.save(WatchState(name="jobs", chat_ids=["1"], started_at=BASE), "auth")
    store.close()
    with sqlite3.connect(store.path) as db:
        if damage == "version":
            db.execute("PRAGMA user_version=99")
        else:
            db.execute("UPDATE watches SET payload='broken'")
    before = store.path.read_bytes()
    reopened = WatchStore(tmp_path / "session", root=tmp_path / "state")
    try:
        with pytest.raises(ToolError, match="WATCH_INCOMPATIBLE"):
            reopened.get("jobs", "auth")
        assert store.path.read_bytes() == before
    finally:
        reopened.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
def test_watch_storage_is_private_and_refuses_redirection(tmp_path):
    root = tmp_path / "state"
    store = WatchStore(tmp_path / "session", root=root)
    store.save(WatchState(name="jobs", chat_ids=["1"], started_at=BASE), "auth")
    store.close()
    assert root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.path.rename(root / "target")
    store.path.symlink_to(root / "target")
    reopened = WatchStore(tmp_path / "session", root=root)
    try:
        with pytest.raises(ToolError, match="WATCH_STORAGE_ERROR"):
            reopened.get("jobs", "auth")
    finally:
        reopened.close()
