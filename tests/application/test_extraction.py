"""Extraction composes bounded files, immutable snapshots and live permissions."""

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from telegram_mcp.application import extraction
from telegram_mcp.domain.models import ToolError
from tests.support.extraction import extraction_context


async def test_continuation_is_immutable_and_never_downloads_or_parses_again(
    tmp_path: Path,
) -> None:
    async with extraction_context(tmp_path) as (service, backend, _, parser, _, _):
        result = await service.extract_attachment_text("1:10:document", text_limit=3)
        assert result.source_sha256 == hashlib.sha256(backend.data).hexdigest()
        assert result.message_ref.key == "1:10" and result.name == "selected.docx"
        expiry, chunks = result.expires_at, [result.text]
        while result.next_text_offset is not None:
            result = await service.extract_attachment_text(
                extraction_id=result.extraction_id,
                text_offset=result.next_text_offset,
                text_limit=3,
            )
            chunks.append(result.text)
            assert result.expires_at == expiry
        assert "".join(chunks) == parser.document.text
        assert backend.downloads == parser.calls == 1
        assert not list((tmp_path / "downloads").glob("*.bin"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"attachment_id": "1:10:document", "extraction_id": "x"},
        {"attachment_id": "1:10:document", "text_offset": 1},
        {"attachment_id": "bad"},
        {"attachment_id": "1:10:document", "text_limit": 24001},
    ],
)
async def test_invalid_arguments_do_no_work(tmp_path: Path, kwargs: dict) -> None:
    async with extraction_context(tmp_path) as (service, backend, _, parser, store, _):
        with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
            await service.extract_attachment_text(**kwargs)
        assert backend.backend_calls == [] and parser.calls == 0 and not store.root.exists()


@pytest.mark.parametrize("case", ["extra", "downloads", "access", "notification", "photo"])
async def test_gates_prevent_downloads_and_processing(tmp_path: Path, case: str) -> None:
    async with extraction_context(tmp_path) as (service, backend, settings, parser, _, _):
        identifier, code = "1:10:document", "ACCESS_DENIED"
        if case == "extra":
            parser.available, code = False, "EXTRACTION_UNAVAILABLE"
        elif case == "downloads":
            settings.allow_attachment_downloads, code = False, "DOWNLOADS_DISABLED"
        elif case == "access":
            settings.allowed_chat_ids = []
        elif case == "notification":
            identifier = "777000:10:document"
        else:
            identifier, code = "1:10:photo", "DOCUMENT_UNSUPPORTED"
        with pytest.raises(ToolError, match=code):
            await service.extract_attachment_text(identifier)
        assert backend.downloads == parser.calls == 0


@pytest.mark.parametrize("case", ["policy", "identity", "protected", "network", "authorization"])
async def test_rechecks_withhold_saved_text_and_preserve_retry(tmp_path: Path, case: str) -> None:
    async with extraction_context(tmp_path) as (service, backend, settings, _, _, _):
        result = await service.extract_attachment_text("1:10:document")
        original = backend.source
        if case == "policy":
            settings.allowed_chat_ids = []
            code = "ACCESS_DENIED"
        elif case == "identity":
            backend.source = replace(original, identity="replacement")
            code = "ATTACHMENT_UNAVAILABLE"
        else:
            code = {
                "protected": "ATTACHMENT_PROTECTED",
                "network": "CONNECTION_ERROR",
                "authorization": "AUTH_REQUIRED",
            }[case]
            backend.prepare_error = ToolError(code, "Synthetic failure")
        with pytest.raises(ToolError, match=code):
            await service.extract_attachment_text(extraction_id=result.extraction_id)
        settings.allowed_chat_ids, backend.source, backend.prepare_error = ["1"], original, None
        replay = await service.extract_attachment_text(extraction_id=result.extraction_id)
        assert replay == result and backend.downloads == 1


async def test_revocation_during_parsing_prevents_first_delivery(tmp_path: Path) -> None:
    async with extraction_context(tmp_path) as (service, _, settings, parser, store, _):
        parser.resume = asyncio.Event()
        task = asyncio.create_task(service.extract_attachment_text("1:10:document"))
        await parser.entered.wait()
        settings.allow_attachment_downloads = False
        parser.resume.set()
        with pytest.raises(ToolError, match="DOWNLOADS_DISABLED"):
            await task
        assert store._files.used == (0, 0) and not store._reservations


async def test_expiry_is_not_renewed_by_reads(tmp_path: Path) -> None:
    async with extraction_context(tmp_path) as (service, backend, _, _, _, clock):
        result = await service.extract_attachment_text("1:10:document")
        clock.time += 899
        await service.extract_attachment_text(extraction_id=result.extraction_id)
        clock.time += 1
        with pytest.raises(ToolError, match="EXTRACTION_EXPIRED"):
            await service.extract_attachment_text(extraction_id=result.extraction_id)
        assert backend.downloads == 1


@pytest.mark.parametrize(
    "code", ["DOCUMENT_INVALID", "EXTRACTION_LIMIT_EXCEEDED", "EXTRACTION_TIMEOUT"]
)
async def test_parser_failure_publishes_nothing_and_releases_capacity(
    tmp_path: Path, code: str
) -> None:
    async with extraction_context(tmp_path) as (service, _, _, parser, store, _):
        parser.error = ToolError(code, "Synthetic failure")
        with pytest.raises(ToolError, match=code):
            await service.extract_attachment_text("1:10:document")
        assert store._files.used == (0, 0) and not store._reservations
        parser.error = None
        assert (await service.extract_attachment_text("1:10:document")).text


async def test_other_tools_remain_available_and_shutdown_drains_work(tmp_path: Path) -> None:
    async with extraction_context(tmp_path) as (service, _, _, parser, store, _):
        parser.resume = asyncio.Event()
        task = asyncio.create_task(service.extract_attachment_text("1:10:document"))
        await parser.entered.wait()
        status = await asyncio.wait_for(service.get_status(), 1)
        assert status["text_extraction_available"] is True
        queued = asyncio.create_task(service.extract_attachment_text("1:10:document"))
        await asyncio.sleep(0)
        await service.close()
        assert task.cancelled() and queued.cancelled()
        assert parser.closed and not store._reservations and not store._files.entries


async def test_call_deadline_releases_capacity_without_publishing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(extraction, "EXTRACTION_TIMEOUT_SECONDS", 0.05)
    async with extraction_context(tmp_path) as (service, _, _, parser, store, _):
        parser.resume = asyncio.Event()
        with pytest.raises(ToolError, match="EXTRACTION_TIMEOUT"):
            await service.extract_attachment_text("1:10:document")
        assert not store._reservations and not store._files.entries


async def test_queued_deadline_does_not_cancel_the_active_extraction(tmp_path: Path, monkeypatch):
    async with extraction_context(tmp_path) as (service, _, _, parser, _, _):
        parser.resume = asyncio.Event()
        active = asyncio.create_task(service.extract_attachment_text("1:10:document"))
        await parser.entered.wait()
        monkeypatch.setattr(extraction, "EXTRACTION_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(ToolError, match="SERVER_BUSY"):
            await service.extract_attachment_text("1:10:document")
        assert parser.calls == 1 and not active.done()
        parser.resume.set()
        assert (await active).text == parser.document.text


async def test_extraction_does_not_remove_a_download_owned_by_another_call(tmp_path: Path) -> None:
    async with extraction_context(tmp_path) as (service, backend, _, _, _, _):
        download = await service.download_attachment("1:10:document")
        await service.extract_attachment_text("1:10:document")
        assert (await service.read_download_resource(download.resource_uri))[0] == backend.data


async def test_initial_extraction_checks_source_twice_then_once_per_continuation(tmp_path: Path):
    async with extraction_context(tmp_path) as (service, backend, _, _, _, _):
        result = await service.extract_attachment_text("1:10:document", text_limit=3)
        assert backend.backend_calls.count("prepare_attachment") == 2
        assert backend.downloads == 1
        await service.extract_attachment_text(extraction_id=result.extraction_id, text_offset=3)
        assert backend.backend_calls.count("prepare_attachment") == 3
        assert backend.downloads == 1


@pytest.mark.parametrize("stage", ["download", "parse"])
@pytest.mark.parametrize("change", ["access", "identity", "protection", "authorization"])
async def test_source_changes_during_extraction_prevent_publication(
    tmp_path: Path, stage: str, change: str
):
    async with extraction_context(tmp_path) as (service, backend, settings, parser, store, _):
        paused = backend if stage == "download" else parser
        paused.resume = asyncio.Event()
        task = asyncio.create_task(service.extract_attachment_text("1:10:document"))
        await paused.entered.wait()
        if change == "access":
            settings.allowed_chat_ids = []
            code = "ACCESS_DENIED"
        elif change == "identity":
            backend.source = replace(backend.source, identity="replacement")
            code = "ATTACHMENT_UNAVAILABLE"
        else:
            code = "ATTACHMENT_PROTECTED" if change == "protection" else "AUTH_REQUIRED"
            backend.prepare_error = ToolError(code, "Synthetic failure")
        paused.resume.set()
        with pytest.raises(ToolError, match=code):
            await task
        assert store._files.used == (0, 0) and not store._reservations
        assert not list((tmp_path / "downloads").glob("*.bin"))
