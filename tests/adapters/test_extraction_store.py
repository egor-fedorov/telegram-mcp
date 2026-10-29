"""Private immutable extraction cache never evicts a live result."""

import asyncio
import os
import threading
from pathlib import Path

import pytest

from telegram_mcp.adapters.extraction import store as storage
from telegram_mcp.domain.models import ToolError
from tests.support.extraction import extraction_context


async def test_capacity_preserves_live_results_and_expiry_releases_space(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_EXTRACTION_FILES", 1)
    async with extraction_context(tmp_path) as (service, backend, _, _, _, clock):
        first = await service.extract_attachment_text("1:10:document")
        with pytest.raises(ToolError, match="EXTRACTION_STORAGE_FULL"):
            await service.extract_attachment_text("1:10:document")
        assert backend.downloads == 1
        assert (
            await service.extract_attachment_text(extraction_id=first.extraction_id)
        ).text == first.text
        clock.time += 900
        second = await service.extract_attachment_text("1:10:document")
        assert second.extraction_id != first.extraction_id


async def test_modified_snapshot_bytes_are_not_returned(tmp_path: Path) -> None:
    async with extraction_context(tmp_path) as (service, _, _, _, store, _):
        result = await service.extract_attachment_text("1:10:document")
        path = store.root / f"{result.extraction_id}.json"
        path.write_text("tampered", encoding="utf-8")
        with pytest.raises(ToolError, match="EXTRACTION_EXPIRED"):
            await service.extract_attachment_text(extraction_id=result.extraction_id)


async def test_snapshot_is_private_and_removed_on_shutdown(tmp_path: Path) -> None:
    async with extraction_context(tmp_path) as (service, _, _, _, store, _):
        result = await service.extract_attachment_text("1:10:document")
        path = store.root / f"{result.extraction_id}.json"
        assert path.exists()
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    assert not path.exists()


async def test_cancelled_write_finishes_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    entered, resume = threading.Event(), threading.Event()
    original = storage.ExtractionStore._write

    def delayed(part, path, data):
        entered.set()
        assert resume.wait(5)
        original(part, path, data)

    monkeypatch.setattr(storage.ExtractionStore, "_write", staticmethod(delayed))
    async with extraction_context(tmp_path) as (service, _, _, _, store, _):
        task = asyncio.create_task(service.extract_attachment_text("1:10:document"))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(store.root.glob("*.json")) and not list(store.root.glob("*.part"))
        assert not store._reservations and store._files.used == (0, 0)
