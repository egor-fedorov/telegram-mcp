"""Account isolation, durable quotas and non-redirected local uploads."""

import os

import pytest

from telegram_mcp.adapters.config import load_settings
from telegram_mcp.adapters.local_uploads import read_allowed_file
from telegram_mcp.adapters.outbound_store import OutboundStore
from telegram_mcp.domain.models import ToolError
from telegram_mcp.domain.outbound import OutboundOperation, SendReceipt
from tests.support.fakes import BASE


async def test_files_are_private_account_bound_and_integrity_checked(tmp_path):
    store = OutboundStore(tmp_path / "account.session")
    try:
        file = await store.register("one", "../cv.pdf", "application/pdf", b"CV", BASE)
        assert file.name == "cv.pdf" and (await store.file("one", file.file_id))[1] == b"CV"
        assert not await store.files("two")
        with pytest.raises(ToolError, match="FILE_UNAVAILABLE"):
            await store.file("two", file.file_id)
        if os.name == "posix":
            assert store.path.stat().st_mode & 0o777 == 0o600
        import sqlite3

        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE files SET data=?", (b"Changed",))
        with pytest.raises(ToolError, match="FILE_UNAVAILABLE"):
            await store.file("one", file.file_id)
    finally:
        await store.close()


async def test_wide_upload_directory_excludes_server_credentials_and_state(tmp_path):
    config = tmp_path / "custom.toml"
    config.write_text("", encoding="utf-8")
    settings = load_settings(config)
    session = tmp_path / "selected.session"
    settings.http_token_path = tmp_path / "custom-token"
    settings.archive_directory = tmp_path / "saved"
    settings.archive_directory.mkdir()
    store = OutboundStore(session, (tmp_path,), private_paths=settings.private_upload_paths())
    try:
        paths = [
            config,
            session,
            tmp_path / "selected.session-wal",
            settings.http_token_path,
            store.path,
            settings.archive_directory / "copy",
        ]
        for path in paths:
            path.write_bytes(b"synthetic")
            with pytest.raises(ToolError, match="FILE_ACCESS_DENIED"):
                await store.import_local("one", str(path), BASE)
        assert not store.path.with_name(store.path.name + "-journal").exists()
    finally:
        await store.close()


def test_local_upload_rejects_hard_link_alias(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"synthetic")
    alias = tmp_path / "alias"
    try:
        alias.hardlink_to(original)
    except OSError:
        pytest.skip("Hard links are unavailable on this filesystem")
    with pytest.raises(ToolError, match="FILE_ACCESS_DENIED"):
        read_allowed_file(alias, (tmp_path,), 100, (original,))


async def test_quota_never_evicts_registered_files(tmp_path, monkeypatch):
    monkeypatch.setattr("telegram_mcp.adapters.outbound_store.MAX_FILES", 1)
    store = OutboundStore(tmp_path / "account.session")
    try:
        first = await store.register("one", "first", "text/plain", b"First", BASE)
        with pytest.raises(ToolError, match="FILE_STORAGE_FULL"):
            await store.register("one", "second", "text/plain", b"Second", BASE)
        assert len(await store.files("one")) == 1
        assert (await store.file("one", first.file_id))[1] == b"First"
    finally:
        await store.close()


async def test_operation_quota_preserves_old_keys_and_payload_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("telegram_mcp.adapters.outbound_store.MAX_OPERATIONS", 1)
    store = OutboundStore(tmp_path / "account.session")
    op = OutboundOperation(
        operation_id="one",
        account_id="one",
        idempotency_key="one",
        kind="message",
        request_hash="hash",
        payload={"text": "secret body"},
        chat_id="1",
        random_id=123,
        receipt=SendReceipt(operation_id="one", status="sent", chat_id="1"),
    )
    try:
        await store.create_operation(op)
        with pytest.raises(ToolError, match="OPERATION_STORAGE_FULL"):
            await store.create_operation(
                op.model_copy(update={"operation_id": "two", "idempotency_key": "two"})
            )
        assert (await store.create_operation(op)).operation_id == "one"
        assert await store.prune_payloads() == 1
        retained = await store.operation("one", "one")
        assert retained is not None
        assert (
            retained.random_id == 123
            and retained.payload == {}
            and retained.receipt.status == "sent"
        )
        assert await store.operation("another-account", "one") is None
    finally:
        await store.close()


def test_local_file_cannot_escape_configured_directory(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    path = allowed / "cv.txt"
    path.write_bytes(b"CV")
    assert read_allowed_file(path, (allowed,), 20) == b"CV"
    with pytest.raises(ToolError, match="FILE_ACCESS_DENIED"):
        read_allowed_file(tmp_path / "outside", (allowed,), 20)
    with pytest.raises(ToolError, match="FILE_ACCESS_DENIED"):
        read_allowed_file(allowed / ".." / "outside", (allowed,), 20)
    with pytest.raises(ToolError, match="FILE_TOO_LARGE"):
        read_allowed_file(path, (allowed,), 1)


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow path checks")
@pytest.mark.parametrize("redirect_parent", [False, True])
def test_upload_refuses_symlinks_in_any_component(tmp_path, redirect_parent):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "cv").write_bytes(b"CV")
    if redirect_parent:
        (allowed / "redirect").symlink_to(target, target_is_directory=True)
        path = allowed / "redirect" / "cv"
    else:
        path = allowed / "cv"
        path.symlink_to(target / "cv")
    with pytest.raises(ToolError, match="FILE_ACCESS_DENIED"):
        read_allowed_file(path, (allowed,), 20)
