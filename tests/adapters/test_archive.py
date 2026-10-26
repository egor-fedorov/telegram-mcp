import asyncio
import hashlib
import os
from datetime import timedelta
from pathlib import Path

import pytest

from telegram_mcp.adapters.archive import ArchiveStore
from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import ToolError
from tests.support.downloads import setup_downloads
from tests.support.fakes import FakeClock, msg
from tests.support.incremental import incremental_fixture


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
        await env.service.get_changes(chat_ref="1")
        with pytest.raises(ToolError, match="CHECKPOINT_STORAGE_FULL"):
            await env.service.get_changes(chat_ref="1")
        env.checkpoints.maximum = 3
        monkeypatch.setattr(env.service._archive, "save", original)
        result = await env.service.get_changes(
            checkpoint=start.next_checkpoint, output="jsonl", save=True
        )
        assert result.export is not None
        assert result.returned_count == 1 and result.export.saved_file is not None


async def test_archive_atomic_write_does_not_overwrite_and_cleans_failure(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    store = ArchiveStore(root, FakeClock())
    data = b"selected bytes"
    digest = hashlib.sha256(data).hexdigest()
    first = await store.save(data, "../../original.bin", digest)
    second = await store.save(data, "../../original.bin", digest)
    assert first.local_path != second.local_path
    assert Path(first.local_path).parent.parent == root
    before = set(root.iterdir())

    def fail(fd):
        raise OSError("Disk full")

    monkeypatch.setattr("telegram_mcp.adapters.archive.os.fsync", fail)
    with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
        await store.save(data, "file.bin", digest)
    assert set(root.iterdir()) == before
    assert (await asyncio.to_thread(Path(first.local_path).read_bytes)) == data


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
async def test_archive_permissions_and_redirected_root(tmp_path):
    root = tmp_path / "archive"
    store = ArchiveStore(root, FakeClock())
    result = await store.save(b"private", "file.txt", hashlib.sha256(b"private").hexdigest())
    assert root.stat().st_mode & 0o777 == 0o700
    assert (await asyncio.to_thread(Path(result.local_path).stat)).st_mode & 0o777 == 0o600
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ToolError, match="ARCHIVE_STORAGE_ERROR"):
        await ArchiveStore(alias, FakeClock()).save(b"private", "file.txt", result.sha256)
