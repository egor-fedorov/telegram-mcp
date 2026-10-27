"""State written by 0.5.0 stays readable and serializes byte for byte."""

import hashlib
import shutil

from telegram_mcp.adapters.archive import ArchiveStore
from telegram_mcp.application.watch_delivery import batch_source
from telegram_mcp.domain.changes import CheckpointState
from telegram_mcp.domain.watches import BatchArchive, PendingDelivery, WatchState, batch_jsonl
from tests.support.fakes import FakeClock
from tests.support.paths import FIXTURES_DIR

STATE = FIXTURES_DIR / "state-0.5"


def payload(name: str) -> str:
    return (STATE / name).read_text(encoding="utf-8")


def saved_batch() -> tuple[WatchState, PendingDelivery, BatchArchive]:
    state = WatchState.model_validate_json(payload("watch.json"))
    assert state.pending is not None and state.pending.archive is not None
    assert state.pending.archive.saved_file is not None
    return state, state.pending, state.pending.archive


def test_watch_state_round_trips_byte_for_byte():
    state, _, _ = saved_batch()
    # WatchStore.save normalizes through model_dump before serializing.
    assert WatchState.model_validate(state.model_dump()).model_dump_json() == payload("watch.json")


def test_checkpoint_state_round_trips_byte_for_byte():
    state = CheckpointState.model_validate_json(payload("checkpoint.json"))
    assert state.seen
    serialized = CheckpointState.model_validate(state.model_dump()).model_dump_json()
    assert serialized == payload("checkpoint.json")


def test_archived_batch_matches_its_pending_delivery():
    state, pending, archive = saved_batch()
    assert archive.saved_file is not None
    data = (STATE / "archive" / archive.key / "file.jsonl").read_bytes()
    assert batch_jsonl(state.name, pending) == data
    assert hashlib.sha256(data).hexdigest() == archive.saved_file.sha256


async def test_archive_store_verifies_a_released_copy(tmp_path):
    state, pending, archive = saved_batch()
    assert archive.saved_file is not None
    shutil.copytree(STATE / "archive", tmp_path / "archive")
    path = tmp_path / "archive" / archive.key / "file.jsonl"
    saved = archive.saved_file.model_copy(
        update={"local_path": str(path), "metadata_path": str(path.parent / "metadata.json")}
    )
    store = ArchiveStore(tmp_path / "archive", FakeClock())
    try:
        assert await store.verify(saved, batch_source(state.name, pending))
    finally:
        await store.close()
