# Test assertions inspect only bounded local temporary files synchronously.
# ruff: noqa: ASYNC230, ASYNC240

import asyncio
import hashlib
import os
import secrets
import stat
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from telegram_mcp.adapters import downloads, private_paths
from telegram_mcp.adapters.downloads import DownloadStore
from telegram_mcp.application.attachment_ports import (
    DOWNLOAD_TTL_SECONDS,
    MAX_ATTACHMENT_BYTES,
    MAX_DOWNLOAD_FILES,
    RESOURCE_PREFIX,
    AttachmentSource,
)
from telegram_mcp.domain.models import MessageRef, ToolError

from .fakes import FakeClock


def source(*, size: int | None = None, name: str = "report.txt") -> AttachmentSource:
    return AttachmentSource(
        attachment_id="document:-100123:42",
        message_ref=MessageRef(chat_id="-100123", message_id=42),
        kind="document",
        identity="synthetic-document-id",
        name=name,
        mime_type="text/plain",
        size=size,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[DownloadStore]:
    instance = DownloadStore(tmp_path / "account.session", clock, root=tmp_path / "downloads")
    try:
        yield instance
    finally:
        await instance.close()


def token(uri: str) -> str:
    return uri.removeprefix(RESOURCE_PREFIX)


async def save(store: DownloadStore, data: bytes = b"synthetic-content"):
    selected = source(size=len(data))
    writer = await store.reserve(selected)
    writer.write(data)
    return await store.commit(writer, selected)


async def test_lookup_and_read_do_not_initialize_storage(store: DownloadStore) -> None:
    for operation in (store.lookup, store.read):
        with pytest.raises(ToolError, match="DOWNLOAD_UNAVAILABLE"):
            await operation("unknown")
    assert not store.root.exists()


async def test_commit_publishes_private_file_with_verified_metadata(
    store: DownloadStore, clock: FakeClock
) -> None:
    data = "synthetic attachment: привет".encode()
    selected = source(size=len(data), name="../../outside.txt")
    writer = await store.reserve(selected)
    writer.write(data)
    assert not list(store.root.glob("*.bin"))
    stored = await store.commit(writer, selected)
    result = stored.result
    path = Path(result.local_path)
    assert path.parent == store.root
    assert path.suffix == ".bin"
    assert len(path.stem) == 43
    assert result.name == "../../outside.txt"
    assert result.message_ref == selected.message_ref
    assert result.attachment_id == selected.attachment_id
    assert stored.identity == selected.identity
    assert result.size == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.mime_type == "text/plain"
    assert result.expires_at == clock.now + timedelta(seconds=DOWNLOAD_TTL_SECONDS)
    assert await store.lookup(token(result.resource_uri)) == stored
    assert await store.read(token(result.resource_uri)) == data
    assert not list(store.root.glob("*.part"))


async def test_declared_oversize_is_rejected_without_creating_storage(store: DownloadStore) -> None:
    with pytest.raises(ToolError, match="ATTACHMENT_TOO_LARGE"):
        await store.reserve(source(size=MAX_ATTACHMENT_BYTES + 1))
    assert not store.root.exists()


@pytest.mark.parametrize("declared_size", [None, 1, MAX_ATTACHMENT_BYTES])
async def test_writer_enforces_actual_cap_and_accepts_exact_limit(
    store: DownloadStore, declared_size: int | None
) -> None:
    selected = source(size=declared_size)
    writer = await store.reserve(selected)
    data = b"x" * MAX_ATTACHMENT_BYTES
    assert writer.write(data) == MAX_ATTACHMENT_BYTES
    with pytest.raises(ToolError, match="ATTACHMENT_TOO_LARGE"):
        writer.write(b"y")
    stored = await store.commit(writer, selected)
    assert await store.read(token(stored.result.resource_uri)) == data


async def test_writer_can_reset_for_one_fresh_download(store: DownloadStore) -> None:
    selected = source()
    writer = await store.reserve(selected)
    writer.write(b"discarded partial bytes")
    writer.seek(0)
    writer.truncate(0)
    writer.write(b"new")
    stored = await store.commit(writer, selected)
    assert stored.result.size == 3
    assert stored.result.sha256 == hashlib.sha256(b"new").hexdigest()
    assert await store.read(token(stored.result.resource_uri)) == b"new"


async def test_seek_and_truncate_cannot_exceed_file_limit(store: DownloadStore) -> None:
    writer = await store.reserve(source())
    with pytest.raises(ToolError, match="ATTACHMENT_TOO_LARGE"):
        writer.seek(MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(ToolError, match="ATTACHMENT_TOO_LARGE"):
        writer.truncate(MAX_ATTACHMENT_BYTES + 1)


async def test_active_reservations_consume_full_size_quota(store: DownloadStore) -> None:
    writers = [await store.reserve(source()) for _ in range(5)]
    with pytest.raises(ToolError, match="DOWNLOAD_STORAGE_FULL"):
        await store.reserve(source())
    await store.discard(writers.pop())
    replacement = await store.reserve(source())
    await store.discard(replacement)
    for writer in writers:
        await store.discard(writer)
    assert not list(store.root.glob("*.part"))


async def test_byte_quota_does_not_evict_previously_issued_downloads(store: DownloadStore) -> None:
    first = await save(store, b"a" * MAX_ATTACHMENT_BYTES)
    for _ in range(4):
        await save(store, b"b" * MAX_ATTACHMENT_BYTES)
    with pytest.raises(ToolError, match="DOWNLOAD_STORAGE_FULL"):
        await store.reserve(source())
    assert await store.read(token(first.result.resource_uri)) == b"a" * MAX_ATTACHMENT_BYTES


async def test_file_count_limit_includes_empty_downloads(store: DownloadStore) -> None:
    for _ in range(MAX_DOWNLOAD_FILES):
        await save(store, b"")
    with pytest.raises(ToolError, match="DOWNLOAD_STORAGE_FULL"):
        await store.reserve(source())


async def test_expiry_denies_reads_before_background_deletion(
    store: DownloadStore, clock: FakeClock
) -> None:
    stored = await save(store)
    path = Path(stored.result.local_path)
    clock.time += DOWNLOAD_TTL_SECONDS
    for operation in (store.lookup, store.read):
        with pytest.raises(ToolError, match="DOWNLOAD_UNAVAILABLE"):
            await operation(token(stored.result.resource_uri))
    assert path.exists()
    # The next reservation also cleans expired files without waiting for the timer.
    writer = await store.reserve(source())
    assert not path.exists()
    await store.discard(writer)


async def test_background_sweep_removes_expired_files_but_preserves_active_writer(
    store: DownloadStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloads, "DOWNLOAD_SWEEP_SECONDS", 0)
    stored = await save(store)
    active = await store.reserve(source())
    active.write(b"partial")
    clock.time += DOWNLOAD_TTL_SECONDS
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not Path(stored.result.local_path).exists()
    assert len(list(store.root.glob("*.part"))) == 1
    active.write(b" more")
    await store.discard(active)


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
async def test_caller_cleanup_removes_partial_downloads(store: DownloadStore, failure: str) -> None:
    started = asyncio.Event()

    async def download() -> None:
        writer = await store.reserve(source())
        try:
            writer.write(b"partial content")
            started.set()
            await asyncio.Event().wait()
        finally:
            await store.discard(writer)

    if failure == "cancel":
        task = asyncio.create_task(download())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                await download()
    assert not list(store.root.glob("*.part"))


async def test_second_owner_cannot_clean_first_owners_files(
    store: DownloadStore, clock: FakeClock, tmp_path: Path
) -> None:
    stored = await save(store)
    second = DownloadStore(tmp_path / "account.session", clock, root=store.root)
    try:
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            await second.reserve(source())
        assert Path(stored.result.local_path).exists()
        assert await store.read(token(stored.result.resource_uri)) == b"synthetic-content"
    finally:
        await second.close()


async def test_start_cleans_only_managed_crash_leftovers(store: DownloadStore) -> None:
    store.root.mkdir()
    paths = [store.root / (secrets.token_urlsafe(32) + suffix) for suffix in (".part", ".bin")]
    for path in paths:
        path.write_bytes(b"stale")
    unrelated = store.root / "unrelated.txt"
    unrelated.write_bytes(b"preserve")
    writer = await store.reserve(source())
    assert not any(path.exists() for path in paths)
    assert unrelated.read_bytes() == b"preserve"
    await store.discard(writer)


async def test_close_cleans_files_and_keeps_lock_inode_for_next_owner(
    store: DownloadStore, clock: FakeClock, tmp_path: Path
) -> None:
    stored = await save(store)
    writer = await store.reserve(source())
    writer.write(b"unfinished")
    lock_stat = (store.root / ".lock").stat()
    await store.close()
    assert not Path(stored.result.local_path).exists()
    assert not list(store.root.glob("*.part"))
    assert (store.root / ".lock").stat().st_ino == lock_stat.st_ino
    next_owner = DownloadStore(tmp_path / "account.session", clock, root=store.root)
    try:
        await save(next_owner)
    finally:
        await next_owner.close()


async def test_failed_delete_keeps_quota_until_retry_succeeds(
    store: DownloadStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(5):
        await save(store, b"x" * MAX_ATTACHMENT_BYTES)
    original = Path.unlink

    def busy(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".bin":
            raise PermissionError("Synthetic Windows sharing violation")
        original(path, missing_ok=missing_ok)

    clock.time += DOWNLOAD_TTL_SECONDS
    monkeypatch.setattr(Path, "unlink", busy)
    with pytest.raises(ToolError, match="DOWNLOAD_STORAGE_FULL"):
        await store.reserve(source())
    assert len(list(store.root.glob("*.bin"))) == 5
    monkeypatch.setattr(Path, "unlink", original)
    writer = await store.reserve(source())
    assert not list(store.root.glob("*.bin"))
    await store.discard(writer)


async def test_failed_partial_delete_remains_accounted(
    store: DownloadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.unlink

    def busy(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".part":
            raise PermissionError("Synthetic Windows sharing violation")
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", busy)
    for _ in range(5):
        writer = await store.reserve(source())
        writer.write(b"x" * MAX_ATTACHMENT_BYTES)
        await store.discard(writer)
    with pytest.raises(ToolError, match="DOWNLOAD_STORAGE_FULL"):
        await store.reserve(source())
    monkeypatch.setattr(Path, "unlink", original)
    writer = await store.reserve(source())
    await store.discard(writer)
    assert not list(store.root.glob("*.part"))


async def test_partial_write_error_does_not_undercount_undeletable_files(
    store: DownloadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_unlink = Path.unlink

    def busy(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".part":
            raise PermissionError("Synthetic sharing violation")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", busy)
    for _ in range(5):
        writer = await store.reserve(source())
        actual = store._owned_writer(writer)
        original_write = actual.stream.write

        def partial_write(data: bytes, write=original_write) -> int:
            write(data[:-1])
            raise OSError("Synthetic failure after writing some bytes")

        monkeypatch.setattr(actual.stream, "write", partial_write)
        with pytest.raises(ToolError, match="LOCAL_IO_ERROR"):
            writer.write(b"x" * MAX_ATTACHMENT_BYTES)
        assert actual.size == 0
        await store.discard(writer)
    with pytest.raises(ToolError, match="DOWNLOAD_STORAGE_FULL"):
        await store.reserve(source())
    monkeypatch.setattr(Path, "unlink", original_unlink)
    writer = await store.reserve(source())
    await store.discard(writer)
    assert not list(store.root.glob("*.part"))


@pytest.mark.parametrize("mutation", ["replace", "grow", "remove"])
async def test_resource_does_not_return_modified_or_missing_local_file(
    store: DownloadStore, mutation: str
) -> None:
    stored = await save(store)
    path = Path(stored.result.local_path)
    if mutation == "replace":
        path.write_bytes(b"different-content")
    elif mutation == "grow":
        with path.open("ab") as stream:
            stream.truncate(MAX_ATTACHMENT_BYTES + 100)
    else:
        path.unlink()
    with pytest.raises(ToolError, match="DOWNLOAD_UNAVAILABLE"):
        await store.read(token(stored.result.resource_uri))


@pytest.mark.skipif(sys.platform == "win32", reason="Requires POSIX permissions and symlinks")
async def test_directory_and_file_are_private_before_content_is_written(
    store: DownloadStore,
) -> None:
    writer = await store.reserve(source())
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    for path in store.root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_size == 0
    writer.write(b"private content")
    await store.discard(writer)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows ACLs")
async def test_windows_private_acl_is_applied_before_writing(store: DownloadStore) -> None:
    from .test_shared_transport import assert_windows_token_acl

    writer = await store.reserve(source())
    assert_windows_token_acl(store.root)
    for path in store.root.iterdir():
        assert_windows_token_acl(path)
        assert path.stat().st_size == 0
    writer.write(b"private content")
    await store.discard(writer)


async def test_permission_failure_leaves_no_attachment_bytes_or_leaked_lock(
    store: DownloadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = downloads.protect_private_path

    def denied(path: Path, *, directory: bool = False) -> None:
        if path.name == ".lock":
            raise PermissionError("synthetic private path")
        original(path, directory=directory)

    monkeypatch.setattr(downloads, "protect_private_path", denied)
    with pytest.raises(ToolError, match="LOCAL_IO_ERROR") as caught:
        await store.reserve(source())
    assert "synthetic private path" not in str(caught.value)
    assert not list(store.root.glob("*.part"))
    monkeypatch.setattr(downloads, "protect_private_path", original)
    writer = await store.reserve(source())
    await store.discard(writer)


@pytest.mark.skipif(sys.platform == "win32", reason="Symlink creation requires Windows privileges")
async def test_symlink_cache_directory_is_rejected(tmp_path: Path, clock: FakeClock) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "redirect"
    link.symlink_to(target, target_is_directory=True)
    store = DownloadStore(tmp_path / "session", clock, root=link)
    try:
        with pytest.raises(ToolError, match="LOCAL_IO_ERROR"):
            await store.reserve(source())
        assert not list(target.iterdir())
    finally:
        await store.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Symlink creation requires Windows privileges")
async def test_parent_alias_is_canonicalized_without_rejecting_storage(
    tmp_path: Path, clock: FakeClock
) -> None:
    target = tmp_path / "canonical-parent"
    target.mkdir()
    alias = tmp_path / "platform-alias"
    alias.symlink_to(target, target_is_directory=True)
    store = DownloadStore(tmp_path / "session", clock, root=alias / "downloads")
    try:
        stored = await save(store)
        assert store.root == target / "downloads"
        assert Path(stored.result.local_path).parent == target / "downloads"
        assert await store.read(token(stored.result.resource_uri)) == b"synthetic-content"
    finally:
        await store.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Symlink creation requires Windows privileges")
async def test_cleanup_unlinks_managed_symlink_without_touching_target(
    store: DownloadStore, tmp_path: Path
) -> None:
    target = tmp_path / "outside"
    target.write_bytes(b"preserve")
    store.root.mkdir()
    link = store.root / (secrets.token_urlsafe(32) + ".part")
    link.symlink_to(target)
    writer = await store.reserve(source())
    assert target.read_bytes() == b"preserve"
    assert not link.exists()
    await store.discard(writer)


async def test_default_root_uses_cache_and_canonical_session_path(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloads, "user_cache_path", lambda _name: tmp_path / "cache")
    session = tmp_path / "account.session"
    store = DownloadStore(session, clock)
    digest = hashlib.sha256(os.fsencode(session.resolve())).hexdigest()
    assert store.root == tmp_path / "cache" / "attachments" / digest
    assert not store.root.exists()


def test_windows_permission_error_is_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def denied(_path: Path) -> None:
        raise RuntimeError("synthetic private path")

    path = tmp_path / "file"
    path.touch()
    # Replace the helper's platform object rather than the interpreter-wide sys module.
    from types import SimpleNamespace

    monkeypatch.setattr(private_paths, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(private_paths, "protect_windows_path", denied)
    with pytest.raises(OSError, match="Cannot restrict private storage") as caught:
        private_paths.protect_private_path(path)
    assert "synthetic private path" not in str(caught.value)
