import asyncio
import hashlib
import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from telegram_mcp.adapters.archive import ArchiveStore
from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.archiving import ArchiveSource
from telegram_mcp.domain.models import ToolError
from tests.support.downloads import setup_downloads
from tests.support.fakes import FakeClock, msg
from tests.support.incremental import incremental_fixture

SOURCE = ArchiveSource(tool="download_attachment", chat_id="1")


@pytest.fixture
async def archive_store(tmp_path):
    store = ArchiveStore(tmp_path / "archive", FakeClock())
    try:
        yield store
    finally:
        await store.close()


async def save_file(store, data=b"selected bytes", name="../../original.bin"):
    return await store.save(
        data,
        name,
        hashlib.sha256(data).hexdigest(),
        mime_type="application/octet-stream",
        source=SOURCE,
    )


async def test_archive_is_disabled_by_default_before_telegram_work(tmp_path):
    async with incremental_fixture(tmp_path) as env:
        with pytest.raises(ToolError, match="ARCHIVE_DISABLED"):
            await env.service.export_history(chat_ref="1", save=True)
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await env.service.get_changes(chat_ref="1", save=True)
        assert env.backend.backend_calls == []
        assert not (await env.service.get_status())["archive_enabled"]
    async with setup_downloads(tmp_path) as (service, backend, _, _, _):
        with pytest.raises(ToolError, match="ARCHIVE_DISABLED"):
            await service.download_attachment("1:10:document", save=True)
        assert backend.downloads == 0


async def test_export_copies_survive_resource_expiry_and_server_shutdown(tmp_path):
    settings = Settings(allowed_chat_ids=["1"], archive_directory=tmp_path / "archive")
    async with incremental_fixture(tmp_path, settings=settings) as env:
        first = await env.service.export_history(chat_ref="1", save=True)
        assert first.export is not None and first.export.saved_file is not None
        saved = Path(first.export.saved_file.local_path)
        text, _ = await env.service.read_export_resource(first.export.resource_uri)
        assert (await asyncio.to_thread(saved.read_bytes)) == text.encode()
        assert (
            hashlib.sha256(await asyncio.to_thread(saved.read_bytes)).hexdigest()
            == first.export.saved_file.sha256
        )
        baseline = await env.service.get_changes(chat_ref="1")
        env.backend.stored_messages.append(msg("1", 4, "😀 Vacancy"))
        delta = await env.service.get_changes(
            checkpoint=baseline.next_checkpoint, output="markdown", save=True
        )
        assert delta.export is not None and delta.export.saved_file is not None
        assert "😀 Vacancy" in (
            await asyncio.to_thread(
                Path(delta.export.saved_file.local_path).read_text, encoding="utf-8"
            )
        )
        env.clock.time += 901
        env.clock.now += timedelta(seconds=901)
        with pytest.raises(ToolError, match="EXPORT_UNAVAILABLE"):
            await env.service.read_export_resource(first.export.resource_uri)
        assert await asyncio.to_thread(saved.exists)
    assert await asyncio.to_thread(saved.exists)
    assert not (await asyncio.to_thread(Path(first.export.local_path).exists))


async def test_attachment_copy_survives_shutdown_and_archive_failure_releases_cache(
    tmp_path, monkeypatch
):
    async with setup_downloads(tmp_path, archive=True) as (service, backend, _, store, _):
        result = await service.download_attachment("1:10:document", save=True)
        assert result.saved_file is not None
        saved = Path(result.saved_file.local_path)
        assert (await asyncio.to_thread(saved.read_bytes)) == backend.data

        async def fail(*args, **kwargs):
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Disk is full")

        monkeypatch.setattr(service._archive, "save", fail)
        before = len(store._entries)
        with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
            await service.download_attachment("1:10:document", save=True)
        assert len(store._entries) == before
    assert (await asyncio.to_thread(saved.read_bytes)) == backend.data
    assert not (await asyncio.to_thread(Path(result.local_path).exists))


async def test_failed_permanent_export_does_not_retire_input_checkpoint(tmp_path, monkeypatch):
    settings = Settings(allowed_chat_ids=["1"], archive_directory=tmp_path / "archive")
    async with incremental_fixture(tmp_path, settings=settings) as env:
        start = await env.service.get_changes(chat_ref="1")
        env.backend.stored_messages.append(msg("1", 4))
        original = env.service._archive.save

        async def fail(*args, **kwargs):
            raise ToolError("ARCHIVE_STORAGE_ERROR", "Disk is full")

        monkeypatch.setattr(env.service._archive, "save", fail)
        with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
            await env.service.get_changes(
                checkpoint=start.next_checkpoint, output="jsonl", save=True
            )
        # Put quota pressure on the failed request's original position.
        env.checkpoints.maximum = 2
        fresh = await env.service.get_changes(chat_ref="1")
        assert (
            await env.service.get_changes(chat_ref="1")
        ).next_checkpoint == fresh.next_checkpoint
        env.checkpoints.maximum = 3
        monkeypatch.setattr(env.service._archive, "save", original)
        result = await env.service.get_changes(
            checkpoint=start.next_checkpoint, output="jsonl", save=True
        )
        assert result.export is not None
        assert result.returned_count == 1 and result.export.saved_file is not None


async def test_archive_atomic_write_does_not_overwrite_and_cleans_failure(
    tmp_path, monkeypatch, archive_store
):
    root = tmp_path / "archive"
    store = archive_store
    data = b"selected bytes"
    first = await save_file(store, data)
    second = await save_file(store, data)
    assert first.local_path != second.local_path
    assert Path(first.local_path).parent.parent == root
    before = set(root.iterdir())

    def fail(fd):
        raise OSError("Disk full")

    monkeypatch.setattr("telegram_mcp.adapters.archive.os.fsync", fail)
    with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
        await save_file(store, data)
    assert set(root.iterdir()) == before
    assert (await asyncio.to_thread(Path(first.local_path).read_bytes)) == data


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
async def test_archive_permissions_and_redirected_root(tmp_path, archive_store):
    root = tmp_path / "archive"
    store = archive_store
    result = await save_file(store, b"private", "file.txt")
    assert root.stat().st_mode & 0o777 == 0o700
    assert (await asyncio.to_thread(Path(result.local_path).stat)).st_mode & 0o777 == 0o600
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    redirected = ArchiveStore(alias, FakeClock())
    try:
        with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
            await save_file(redirected, b"private", "file.txt")
    finally:
        await redirected.close()


async def test_archive_manifest_preserves_provenance_and_original_unicode_name(archive_store):
    name = "../../Вакансия 😀.pdf"
    saved = await save_file(archive_store, name=name)
    metadata = json.loads(
        await asyncio.to_thread(Path(saved.metadata_path).read_text, encoding="utf-8")
    )
    assert metadata["schema_version"] == 1
    assert metadata["original_name"] == name
    assert metadata["source"] == {"tool": "download_attachment", "chat_id": "1"}
    assert metadata["sha256"] == saved.sha256 and metadata["size"] == saved.size
    assert metadata["file"] == "file.pdf"
    await archive_store.discard(saved)
    assert not Path(saved.local_path).parent.exists()


async def test_archive_fsync_keeps_event_loop_responsive_and_cancel_removes_bundle(
    archive_store, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = os.fsync

    def held(fd):
        entered.set()
        assert release.wait(5)
        original(fd)

    monkeypatch.setattr("telegram_mcp.adapters.archive.os.fsync", held)
    task = asyncio.create_task(save_file(archive_store, b"x" * (20 * 1024 * 1024)))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        assert await asyncio.wait_for(asyncio.sleep(0, result="responsive"), 0.5) == "responsive"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(archive_store.root.iterdir()) == []


async def test_keyed_save_reuses_identical_copy_and_rejects_different_data(tmp_path, archive_store):
    key = "k" * 43
    data = b'{"type":"watch_batch"}\n'
    digest = hashlib.sha256(data).hexdigest()
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    # An interrupted, never-published attempt for this key is removed, not reused.
    (root / f".{key}.part").mkdir()
    save = archive_store.save
    first = await save(
        data, "jobs.jsonl", digest, mime_type="application/x-ndjson", source=SOURCE, key=key
    )
    again = await save(
        data, "jobs.jsonl", digest, mime_type="application/x-ndjson", source=SOURCE, key=key
    )
    assert again == first and Path(first.local_path).parent.name == key
    assert not (root / f".{key}.part").exists()
    other = b"different"
    with pytest.raises(ToolError, match="ARCHIVE_COPY_INVALID"):
        await save(
            other,
            "jobs.jsonl",
            hashlib.sha256(other).hexdigest(),
            mime_type="application/x-ndjson",
            source=SOURCE,
            key=key,
        )
    with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
        await save(
            data, "jobs.jsonl", digest, mime_type="application/x-ndjson", source=SOURCE, key="../x"
        )


async def test_verify_detects_missing_or_changed_copies(archive_store):
    saved = await save_file(archive_store, b"selected bytes")
    assert await archive_store.verify(saved)
    await asyncio.to_thread(Path(saved.local_path).write_bytes, b"changed bytes!")
    assert not await archive_store.verify(saved)
    await asyncio.to_thread(Path(saved.local_path).unlink)
    assert not await archive_store.verify(saved)


async def test_crash_recovery_and_verification_share_the_sidecar_contract(tmp_path, archive_store):
    key = "r" * 43
    data = b'{"type":"watch_batch"}\n'
    digest = hashlib.sha256(data).hexdigest()
    source = ArchiveSource(tool="get_watch_changes", watch_name="jobs", batch_id="b" * 43)
    other = source.model_copy(update={"batch_id": "c" * 43})
    saved = await archive_store.save(
        data, "jobs.jsonl", digest, mime_type="application/x-ndjson", source=source, key=key
    )
    assert await archive_store.verify(saved, source)
    assert not await archive_store.verify(saved, other)
    metadata = Path(saved.metadata_path)
    manifest = json.loads(await asyncio.to_thread(metadata.read_text, encoding="utf-8"))
    manifest["source"]["batch_id"] = "c" * 43
    await asyncio.to_thread(metadata.write_text, json.dumps(manifest), encoding="utf-8")
    assert not await archive_store.verify(saved, source)
    with pytest.raises(ToolError, match="ARCHIVE_COPY_INVALID"):
        await archive_store.save(
            data, "jobs.jsonl", digest, mime_type="application/x-ndjson", source=source, key=key
        )
