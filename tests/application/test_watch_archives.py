"""Permanent watch-batch copies are published before any acknowledgement can commit."""

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from telegram_mcp.adapters.archive import ArchiveStore
from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import BASE, FakeBackend, msg
from tests.support.incremental import incremental_fixture


def archive_settings(path: Path) -> Settings:
    return Settings(allowed_chat_ids=["1"], archive_directory=path / "archive")


def batch_copies(path: Path) -> list[Path]:
    return sorted((path / "archive").glob("*/file.jsonl"))


def file_bytes(file_path: str) -> bytes:
    return Path(file_path).read_bytes()


def file_exists(file_path: str) -> bool:
    return Path(file_path).is_file()


def remove(file_path: str) -> None:
    Path(file_path).unlink()


def read_copy(file_path: str) -> tuple[list[dict], dict]:
    path = Path(file_path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    metadata = json.loads((path.parent / "metadata.json").read_text(encoding="utf-8"))
    return records, metadata


async def test_saved_batch_is_independently_readable_before_acknowledgement(tmp_path):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        page = await env.service.get_watch_changes("jobs", save=True)
        assert page.batch_id and page.saved_file is not None
        records, metadata = read_copy(page.saved_file.local_path)
        data = file_bytes(page.saved_file.local_path)
        assert hashlib.sha256(data).hexdigest() == page.saved_file.sha256 == metadata["sha256"]
        assert metadata["source"]["tool"] == "get_watch_changes"
        assert metadata["source"]["watch_name"] == "jobs"
        assert metadata["source"]["batch_id"] == page.batch_id
        assert metadata["source"]["source_ids"] == ["1"]
        header, *changes = records
        assert header["type"] == "watch_batch" and header["schema_version"] == 1
        assert header["batch_id"] == page.batch_id and header["returned_count"] == 3
        assert [c["message_ref"]["message_id"] for c in changes] == [
            c.message_ref.message_id for c in page.changes
        ]
        assert all(c["message"]["text"] == "budget discussion" for c in changes)
        assert "saved_file" not in json.dumps(records)
        await env.service.ack_watch_changes("jobs", page.batch_id)
        assert file_exists(page.saved_file.local_path)


async def test_acknowledge_and_poll_archives_the_acknowledged_batch_first(tmp_path):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs", limit=2)
        assert first.batch_id and first.saved_file is None
        second = await env.service.get_watch_changes(
            "jobs", limit=2, ack_batch_id=first.batch_id, save=True
        )
        copies = batch_copies(tmp_path)
        assert len(copies) == 2 and second.saved_file is not None
        assert first.batch_id is not None and second.batch_id is not None
        acknowledged = [read_copy(str(path))[1]["source"]["batch_id"] for path in copies]
        assert sorted(acknowledged) == sorted([first.batch_id, second.batch_id])
        # Repeating the old acknowledgement neither re-archives it nor confirms the new batch.
        replay = await env.service.get_watch_changes("jobs", ack_batch_id=first.batch_id, save=True)
        assert replay.batch_id == second.batch_id and len(batch_copies(tmp_path)) == 2
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.pending is not None
        assert state.pending.result.batch_id == second.batch_id


async def test_save_requires_archive_before_acknowledging_or_polling(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs")
        assert first.batch_id
        calls = len(env.backend.backend_calls)
        with pytest.raises(ToolError, match="ARCHIVE_DISABLED"):
            await env.service.get_watch_changes("jobs", ack_batch_id=first.batch_id, save=True)
        assert len(env.backend.backend_calls) == calls
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.pending is not None
        assert state.last_acknowledged is None


async def test_failed_save_leaves_batch_pending_and_blocks_both_ack_paths(tmp_path, monkeypatch):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        store = env.service._archive
        assert isinstance(store, ArchiveStore)
        original = store._save

        def disk_full(*args, **kwargs):
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Disk full")

        monkeypatch.setattr(store, "_save", disk_full)
        with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
            await env.service.get_watch_changes("jobs", save=True)
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.pending is not None
        batch_id = state.pending.result.batch_id
        assert batch_id and state.pending.archive and state.pending.archive.saved_file is None
        unsaved = await env.service.get_watch_changes("jobs")
        assert unsaved.batch_id == batch_id and unsaved.saved_file is None
        with pytest.raises(ToolError, match="WATCH_ARCHIVE_PENDING"):
            await env.service.ack_watch_changes("jobs", batch_id)
        with pytest.raises(ToolError, match="WATCH_ARCHIVE_PENDING"):
            await env.service.get_watch_changes("jobs", ack_batch_id=batch_id)
        monkeypatch.setattr(store, "_save", original)
        saved = await env.service.get_watch_changes("jobs", save=True)
        assert saved.batch_id == batch_id and saved.saved_file is not None
        await env.service.ack_watch_changes("jobs", batch_id)


async def test_lost_response_and_restart_reuse_the_same_copy(tmp_path):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs", save=True)
        again = await env.service.get_watch_changes("jobs", save=True)
        assert again == first and len(batch_copies(tmp_path)) == 1
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        replay = await env.service.get_watch_changes("jobs", save=True)
        assert replay == first and len(batch_copies(tmp_path)) == 1
        assert first.batch_id is not None
        await env.service.ack_watch_changes("jobs", first.batch_id)


async def test_crash_between_publication_and_receipt_reconciles_without_new_copy(
    tmp_path, monkeypatch
):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        save = env.watches.save
        writes = 0

        def crash_on_receipt(state, binding):
            nonlocal writes
            writes += 1
            if state.pending and state.pending.archive and state.pending.archive.saved_file:
                raise ToolError("WATCH_STORAGE_ERROR", "Crash before the receipt was saved")
            save(state, binding)

        monkeypatch.setattr(env.watches, "save", crash_on_receipt)
        with pytest.raises(ToolError, match="WATCH_STORAGE_ERROR"):
            await env.service.get_watch_changes("jobs", save=True)
        assert len(batch_copies(tmp_path)) == 1
        monkeypatch.setattr(env.watches, "save", save)
        recovered = await env.service.get_watch_changes("jobs", save=True)
        assert recovered.saved_file is not None and len(batch_copies(tmp_path)) == 1
        assert Path(recovered.saved_file.local_path) == batch_copies(tmp_path)[0]
        assert recovered.batch_id is not None
        await env.service.ack_watch_changes("jobs", recovered.batch_id)


async def test_missing_copy_blocks_acknowledgement_until_rebuilt(tmp_path):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        page = await env.service.get_watch_changes("jobs", save=True)
        assert page.batch_id and page.saved_file
        remove(page.saved_file.local_path)
        with pytest.raises(ToolError, match="ARCHIVE_COPY_INVALID"):
            await env.service.ack_watch_changes("jobs", page.batch_id)
        rebuilt = await env.service.get_watch_changes("jobs", save=True)
        assert rebuilt.batch_id == page.batch_id and rebuilt.saved_file is not None
        assert rebuilt.saved_file.local_path != page.saved_file.local_path
        assert rebuilt.saved_file.sha256 == page.saved_file.sha256
        await env.service.ack_watch_changes("jobs", page.batch_id)


async def test_revoked_or_protected_sources_are_not_archived(tmp_path):
    settings = archive_settings(tmp_path)
    async with incremental_fixture(tmp_path, settings=settings) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs")
        assert first.batch_id
        env.backend.export_source_error = ToolError("EXPORT_PROTECTED", "Protected source")
        with pytest.raises(ToolError, match="EXPORT_PROTECTED"):
            await env.service.get_watch_changes("jobs", save=True)
        env.backend.export_source_error = None
        settings.allowed_chat_ids = []
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await env.service.get_watch_changes("jobs", save=True)
        assert batch_copies(tmp_path) == []


async def test_later_save_failure_does_not_roll_back_committed_acknowledgement(
    tmp_path, monkeypatch
):
    backend = FakeBackend([msg("1", mid) for mid in range(1, 5)])
    async with incremental_fixture(
        tmp_path, backend=backend, settings=archive_settings(tmp_path)
    ) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        first = await env.service.get_watch_changes("jobs", limit=2, save=True)
        assert first.batch_id
        store = env.service._archive
        assert isinstance(store, ArchiveStore)

        def disk_full(*args, **kwargs):
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Disk full")

        monkeypatch.setattr(store, "_save", disk_full)
        with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
            await env.service.get_watch_changes(
                "jobs", limit=2, ack_batch_id=first.batch_id, save=True
            )
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.last_acknowledged == first.batch_id
        assert state.pending is not None and state.pending.result.batch_id != first.batch_id


async def test_cancelled_save_keeps_the_batch_pending(tmp_path, monkeypatch):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        store = env.service._archive
        assert isinstance(store, ArchiveStore)
        original = store._save
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            time.sleep(0.2)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "_save", slow)
        task = asyncio.create_task(env.service.get_watch_changes("jobs", save=True))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert batch_copies(tmp_path) == []
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.pending is not None
        assert state.pending.archive and state.pending.archive.saved_file is None
        monkeypatch.setattr(store, "_save", original)
        saved = await env.service.get_watch_changes("jobs", save=True)
        assert saved.saved_file is not None and len(batch_copies(tmp_path)) == 1


async def test_save_false_watches_keep_the_previous_contract(tmp_path):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        page = await env.service.get_watch_changes("jobs")
        assert page.batch_id and page.saved_file is None
        await env.service.ack_watch_changes("jobs", page.batch_id)
        assert batch_copies(tmp_path) == []


def corrupt_sidecar(metadata_path: str, kind: str) -> None:
    path = Path(metadata_path)
    if kind == "malformed":
        path.write_text("{", encoding="utf-8")
        return
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if kind == "empty":
        manifest = {}
    elif kind == "missing_checksum":
        del manifest["sha256"]
    elif kind == "other_batch":
        manifest["source"]["batch_id"] = "x" * 43
    elif kind == "other_watch":
        manifest["source"]["watch_name"] = "news"
    elif kind == "wrong_size":
        manifest["size"] += 1
    elif kind == "naive_time":
        manifest["saved_at"] = "2026-10-27T08:00:00"
    path.write_text(json.dumps(manifest), encoding="utf-8")


def alter_copy(file_path: str, kind: str) -> None:
    path = Path(file_path)
    if kind == "deleted":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"\n")


SIDECAR_DEFECTS = [
    "malformed",
    "empty",
    "missing_checksum",
    "other_batch",
    "other_watch",
    "wrong_size",
    "naive_time",
]


@pytest.mark.parametrize("defect", SIDECAR_DEFECTS)
@pytest.mark.parametrize("ack_path", ["ack_watch_changes", "ack_batch_id"])
async def test_invalid_sidecar_blocks_acknowledgement_until_repaired(tmp_path, defect, ack_path):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        page = await env.service.get_watch_changes("jobs", save=True)
        assert page.batch_id and page.saved_file
        corrupt_sidecar(page.saved_file.metadata_path, defect)
        with pytest.raises(ToolError, match="ARCHIVE_COPY_INVALID"):
            if ack_path == "ack_watch_changes":
                await env.service.ack_watch_changes("jobs", page.batch_id)
            else:
                await env.service.get_watch_changes("jobs", ack_batch_id=page.batch_id)
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.pending is not None
        assert state.pending.result.batch_id == page.batch_id
        assert state.last_acknowledged is None
        repaired = await env.service.get_watch_changes("jobs", save=True)
        assert repaired.batch_id == page.batch_id and repaired.saved_file is not None
        assert repaired.saved_file.local_path != page.saved_file.local_path
        _, metadata = read_copy(repaired.saved_file.local_path)
        assert metadata["source"]["batch_id"] == page.batch_id
        assert metadata["source"]["watch_name"] == "jobs"
        if ack_path == "ack_watch_changes":
            await env.service.ack_watch_changes("jobs", page.batch_id)
        else:
            await env.service.get_watch_changes("jobs", ack_batch_id=page.batch_id)
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.last_acknowledged == page.batch_id


@pytest.mark.parametrize("defect", ["deleted", "altered", "sidecar"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_default_replay_reports_an_invalid_copy_instead_of_returning_it(
    tmp_path, defect, explicit
):
    async with incremental_fixture(tmp_path, settings=archive_settings(tmp_path)) as env:
        await env.service.create_watch("jobs", chat_refs=["1"], date_from=BASE)
        page = await env.service.get_watch_changes("jobs", save=True)
        assert page.batch_id and page.saved_file
        intact = await env.service.get_watch_changes("jobs")
        assert intact == page
        if defect == "sidecar":
            corrupt_sidecar(page.saved_file.metadata_path, "other_batch")
        else:
            alter_copy(page.saved_file.local_path, defect)
        replay = await (
            env.service.get_watch_changes("jobs", save=False)
            if explicit
            else env.service.get_watch_changes("jobs")
        )
        assert replay.batch_id == page.batch_id and replay.changes == page.changes
        assert replay.saved_file is None
        assert replay.issues[0].code == "ARCHIVE_COPY_INVALID"
        assert replay.issue_count == page.issue_count + 1
        state = env.watches.get("jobs", env.backend.binding)
        assert state is not None and state.pending is not None
        assert state.pending.result.issues == page.issues  # the warning is not persisted
        with pytest.raises(ToolError, match="ARCHIVE_COPY_INVALID"):
            await env.service.ack_watch_changes("jobs", page.batch_id)
        repaired = await env.service.get_watch_changes("jobs", save=True)
        assert repaired.saved_file is not None and repaired.issues == page.issues
        await env.service.ack_watch_changes("jobs", page.batch_id)
