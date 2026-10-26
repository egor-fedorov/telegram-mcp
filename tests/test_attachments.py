"""Selected downloads compose current access, storage, retries and resource reads."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from telegram_mcp.adapters.config import Settings, load_settings, save_settings
from telegram_mcp.adapters.downloads import DownloadStore
from telegram_mcp.application.attachment_ports import (
    MAX_ATTACHMENT_BYTES,
    AttachmentKind,
    AttachmentSource,
    AttachmentWriter,
)
from telegram_mcp.application.service import Service
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import Chat, Folder, MessageRef, ToolError
from tests.fakes import FakeBackend, FakeClock


class AttachmentBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.data = b"Selected original attachment\x00\xff"
        self.source = AttachmentSource(
            "1:10:document",
            MessageRef(chat_id="1", message_id=10),
            "document",
            "doc:123",
            "report.bin",
            "application/octet-stream",
            len(self.data),
        )
        self.prepare_error: ToolError | None = None
        self.download_error: ToolError | None = None
        self.downloads = 0
        self.entered = asyncio.Event()
        self.resume: asyncio.Event | None = None

    async def prepare_attachment(self, ref: MessageRef, kind: AttachmentKind) -> AttachmentSource:
        self.backend_calls.append("prepare_attachment")
        if self.prepare_error:
            raise self.prepare_error
        return replace(self.source, attachment_id=f"{ref.key}:{kind}", message_ref=ref, kind=kind)

    async def download_attachment(self, source: AttachmentSource, writer: AttachmentWriter) -> None:
        self.backend_calls.append("download_attachment")
        self.downloads += 1
        writer.write(self.data)
        self.entered.set()
        if self.resume is not None:
            await self.resume.wait()
        if self.download_error:
            raise self.download_error


@asynccontextmanager
async def setup_downloads(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Service, AttachmentBackend, Settings, DownloadStore, FakeClock]]:
    backend = AttachmentBackend()
    settings = Settings(allow_attachment_downloads=True, allowed_chat_ids=["1"])
    clock = FakeClock()
    store = DownloadStore(tmp_path / "account.session", clock, root=tmp_path / "downloads")
    service = build_service(backend, settings, clock=clock, downloads=store)
    try:
        yield service, backend, settings, store, clock
    finally:
        await service.close()


def test_attachment_permission_defaults_off_and_roundtrips(tmp_path: Path) -> None:
    assert not Settings().allow_attachment_downloads
    path = tmp_path / "config.toml"
    save_settings(Settings(allow_attachment_downloads=True), path)
    assert load_settings(path).allow_attachment_downloads


async def test_selected_download_and_resource_preserve_original_bytes(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, _, _, _):
        result = await service.download_attachment("1:10:document")
        assert result.message_ref.key == "1:10"
        assert result.size == len(backend.data)
        assert result.sha256 == hashlib.sha256(backend.data).hexdigest()
        assert await asyncio.to_thread(Path(result.local_path).read_bytes) == backend.data
        for _ in range(2):
            assert await service.read_download_resource(result.resource_uri) == (
                backend.data,
                "application/octet-stream",
            )
        assert backend.downloads == 1
        assert backend.backend_calls.count("authorization") == 3
        assert backend.backend_calls.count("prepare_attachment") == 3
        path = Path(result.local_path)
    assert not await asyncio.to_thread(path.exists)


@pytest.mark.parametrize("resource", [False, True])
async def test_disabled_downloads_do_not_touch_telegram_or_disk(
    tmp_path: Path, resource: bool
) -> None:
    async with setup_downloads(tmp_path) as (service, backend, settings, _, _):
        settings.allow_attachment_downloads = False
        with pytest.raises(ToolError, match="DOWNLOADS_DISABLED"):
            if resource:
                await service.read_download_resource("telegram-attachment://downloads/" + "a" * 43)
            else:
                await service.download_attachment("1:10:document")
        assert backend.backend_calls == []
        assert not (tmp_path / "downloads").exists()


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "https://t.me/example/10",
        "1:10",
        "1:0:photo",
        "1:10:preview",
        "1:2147483648:document",
        "01:10:photo",
    ],
)
async def test_invalid_attachment_id_has_no_backend_effect(tmp_path: Path, identifier: str) -> None:
    async with setup_downloads(tmp_path) as (service, backend, _, _, _):
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await service.download_attachment(identifier)
        assert backend.backend_calls == []


async def test_download_permission_does_not_grant_source_access(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, settings, _, _):
        settings.allowed_chat_ids = []
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await service.download_attachment("1:10:document")
        assert backend.backend_calls == ["authorization"]


async def test_explicit_public_source_is_rechecked_before_resource_output(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, settings, _, _):
        settings.allowed_chat_ids = []
        settings.allow_public_channel_reads = True
        backend.external_chats = [
            Chat(id="-1001", title="Source", type="channel", username="source")
        ]
        result = await service.download_attachment("-1001:10:document")
        assert backend.public_checks == ["-1001"]
        backend.public_faults["-1001"] = ToolError("ACCESS_DENIED", "Source became private.")
        with pytest.raises(ToolError, match="ACCESS_DENIED"):
            await service.read_download_resource(result.resource_uri)
        assert backend.public_checks == ["-1001", "-1001"]
        assert backend.downloads == 1


@pytest.mark.parametrize(
    "failure", ["access", "disabled", "authorization", "protection", "changed"]
)
async def test_resource_rechecks_source_and_policy(tmp_path: Path, failure: str) -> None:
    async with setup_downloads(tmp_path) as (service, backend, settings, _, _):
        result = await service.download_attachment("1:10:document")
        expected = "ATTACHMENT_UNAVAILABLE"
        if failure == "access":
            settings.allowed_chat_ids = []
            expected = "ACCESS_DENIED"
        elif failure == "disabled":
            settings.allow_attachment_downloads = False
            expected = "DOWNLOADS_DISABLED"
        elif failure == "authorization":
            backend.authorization_fault = ToolError("AUTH_REQUIRED", "Revoked.")
            expected = "AUTH_REQUIRED"
        elif failure == "protection":
            backend.prepare_error = ToolError("ATTACHMENT_PROTECTED", "Protected.")
            expected = "ATTACHMENT_PROTECTED"
        else:
            backend.source = replace(backend.source, identity="doc:456")
        with pytest.raises(ToolError, match=expected):
            await service.read_download_resource(result.resource_uri)
        assert backend.downloads == 1


async def test_failed_catalog_check_keeps_resource_for_retry(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, settings, _, _):
        result = await service.download_attachment("1:10:document")
        settings.allowed_folder_ids = [1]
        backend.folders = [Folder(id=1, title="Allowed", position=0, included_chat_ids=["1"])]
        backend.catalog_fault = ToolError("CONNECTION_ERROR", "Offline.")
        with pytest.raises(ToolError, match="CONNECTION_ERROR"):
            await service.read_download_resource(result.resource_uri)
        backend.catalog_fault = None
        assert (await service.read_download_resource(result.resource_uri))[0] == backend.data
        assert backend.downloads == 1


async def test_declared_size_rejected_before_storage_or_download(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, _, _, _):
        backend.source = replace(backend.source, size=MAX_ATTACHMENT_BYTES + 1)
        with pytest.raises(ToolError, match="ATTACHMENT_TOO_LARGE"):
            await service.download_attachment("1:10:document")
        assert backend.downloads == 0
        assert not (tmp_path / "downloads").exists()


async def test_flood_wait_blocks_downloads_and_resource_requests_until_due(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, _, _, clock):
        result = await service.download_attachment("1:10:document")
        backend.download_error = ToolError("RATE_LIMITED", "Wait.", 30)
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await service.download_attachment("1:10:document")
        calls = list(backend.backend_calls)
        clock.time += 5
        for operation in (
            service.download_attachment("1:11:document"),
            service.read_download_resource(result.resource_uri),
        ):
            with pytest.raises(ToolError, match="RATE_LIMITED") as caught:
                await operation
            assert caught.value.retry_after == 25
        assert backend.backend_calls == calls
        backend.download_error = None
        clock.time += 25
        assert (await service.read_download_resource(result.resource_uri))[0] == backend.data
        result2 = await service.download_attachment("1:10:document")
        assert result2.resource_uri != result.resource_uri


@pytest.mark.parametrize("code", ["CONNECTION_ERROR", "LOCAL_IO_ERROR", "TELEGRAM_UNAVAILABLE"])
async def test_failed_download_discards_partial_and_can_retry(tmp_path: Path, code: str) -> None:
    async with setup_downloads(tmp_path) as (service, backend, _, _, _):
        backend.download_error = ToolError(code, "Synthetic failure.")
        with pytest.raises(ToolError, match=code):
            await service.download_attachment("1:10:document")
        assert not list((tmp_path / "downloads").glob("*.part"))
        assert not list((tmp_path / "downloads").glob("*.bin"))
        backend.download_error = None
        result = await service.download_attachment("1:10:document")
        assert await asyncio.to_thread(Path(result.local_path).read_bytes) == backend.data


async def test_cancelled_download_discards_partial(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, _, _, _):
        backend.resume = asyncio.Event()
        task = asyncio.create_task(service.download_attachment("1:10:document"))
        await asyncio.wait_for(backend.entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list((tmp_path / "downloads").glob("*.part"))
        backend.resume.set()
        assert (await service.download_attachment("1:10:document")).size == len(backend.data)


async def test_download_deadline_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("telegram_mcp.application.attachments.DOWNLOAD_TIMEOUT_SECONDS", 0.05)
    async with setup_downloads(tmp_path) as (service, backend, _, _, _):
        backend.resume = asyncio.Event()
        with pytest.raises(ToolError, match="CONNECTION_ERROR"):
            await service.download_attachment("1:10:document")
        assert not list((tmp_path / "downloads").glob("*.part"))


async def test_resource_expiry_does_not_start_an_implicit_download(tmp_path: Path) -> None:
    async with setup_downloads(tmp_path) as (service, backend, _, _, clock):
        result = await service.download_attachment("1:10:document")
        calls = list(backend.backend_calls)
        clock.time += 901
        with pytest.raises(ToolError, match="DOWNLOAD_UNAVAILABLE"):
            await service.read_download_resource(result.resource_uri)
        assert backend.backend_calls == calls
