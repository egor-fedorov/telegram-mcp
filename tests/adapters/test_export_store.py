import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from telegram_mcp.adapters.exports import ExportStore
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import FakeClock


async def test_export_integrity_expiry_cleanup_and_names(tmp_path):
    clock = FakeClock()
    store = ExportStore(tmp_path / "session", clock, root=tmp_path / "exports")
    data = '"😀 source text"\n'.encode()
    result = await store.save(data, "jsonl", "-1001", "auth")
    path = Path(result.local_path)
    assert result.sha256 == hashlib.sha256(data).hexdigest() and result.size == len(data)
    assert path.parent == store.root and path.suffix == ".jsonl"
    assert (await store.lookup(result.resource_uri)).binding == "auth"
    assert await store.read(result.resource_uri) == data
    if os.name != "nt":
        assert (await asyncio.to_thread(path.stat)).st_mode & 0o777 == 0o600
        assert store.root.stat().st_mode & 0o777 == 0o700
    await asyncio.to_thread(path.write_bytes, b"changed")
    with pytest.raises(ToolError, match="EXPORT_UNAVAILABLE"):
        await store.read(result.resource_uri)
    clock.time += 900
    with pytest.raises(ToolError, match="EXPORT_UNAVAILABLE"):
        await store.lookup(result.resource_uri)
    new = await store.save(b"new", "markdown", "1", "auth")
    assert not await asyncio.to_thread(path.exists)
    await store.close()
    assert not await asyncio.to_thread(Path(new.local_path).exists)


async def test_export_quota_releases_after_discard(tmp_path, monkeypatch):
    monkeypatch.setattr("telegram_mcp.adapters.exports.MAX_EXPORT_FILES", 1)
    store = ExportStore(tmp_path / "session", FakeClock(), root=tmp_path / "exports")
    try:
        first = await store.save(b"one", "jsonl", "1", "auth")
        with pytest.raises(ToolError, match="EXPORT_STORAGE_FULL"):
            await store.save(b"two", "jsonl", "1", "auth")
        await store.discard(first.resource_uri)
        second = await store.save(b"two", "jsonl", "1", "auth")
        assert await store.read(second.resource_uri) == b"two"
    finally:
        await store.close()


async def test_restart_removes_only_owned_leftovers_and_enforces_lease(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    orphan = root / ("a" * 43 + ".part")
    orphan.write_bytes(b"incomplete")
    unrelated = root / "keep.txt"
    unrelated.write_text("unrelated")
    store = ExportStore(tmp_path / "session", FakeClock(), root=root)
    second = ExportStore(tmp_path / "session", FakeClock(), root=root)
    try:
        result = await store.save(b"ok", "jsonl", "1", "auth")
        assert not orphan.exists() and unrelated.read_text() == "unrelated"
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            await second.save(b"bad", "jsonl", "1", "auth")
        assert await store.read(result.resource_uri) == b"ok"
    finally:
        await second.close()
        await store.close()


async def test_failed_write_removes_partial_files(tmp_path, monkeypatch):
    store = ExportStore(tmp_path / "session", FakeClock(), root=tmp_path / "exports")

    def failed_sync(_):
        raise OSError("Disk full")

    monkeypatch.setattr("telegram_mcp.adapters.exports.os.fsync", failed_sync)
    try:
        with pytest.raises(ToolError, match="LOCAL_IO_ERROR"):
            await store.save(b"partial", "jsonl", "1", "auth")
        assert list(store.root.glob("*.part")) == []
        assert list(store.root.glob("*.jsonl")) == []
    finally:
        await store.close()


async def test_unknown_resources_and_byte_limits(tmp_path, monkeypatch):
    store = ExportStore(tmp_path / "session", FakeClock(), root=tmp_path / "exports")
    monkeypatch.setattr("telegram_mcp.adapters.exports.MAX_EXPORT_BYTES", 20)
    monkeypatch.setattr("telegram_mcp.adapters.exports.MAX_EXPORT_STORE_BYTES", 25)
    try:
        with pytest.raises(ToolError, match="EXPORT_TOO_LARGE"):
            await store.save(b"x" * 21, "jsonl", "1", "auth")
        await store.save(b"x" * 20, "jsonl", "1", "auth")
        with pytest.raises(ToolError, match="EXPORT_STORAGE_FULL"):
            await store.save(b"x" * 6, "jsonl", "1", "auth")
        for uri in (
            "file:///secret",
            "telegram-export://files/../file",
            "telegram-export://files/" + "a" * 43,
        ):
            with pytest.raises(ToolError, match="EXPORT_UNAVAILABLE"):
                await store.read(uri)
    finally:
        await store.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
async def test_export_store_rejects_redirected_root_and_output(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    redirect = tmp_path / "redirect"
    redirect.symlink_to(target, target_is_directory=True)
    store = ExportStore(tmp_path / "session", FakeClock(), root=redirect)
    try:
        with pytest.raises(ToolError, match="LOCAL_IO_ERROR"):
            await store.save(b"data", "jsonl", "1", "auth")
        assert list(target.iterdir()) == []
    finally:
        await store.close()
