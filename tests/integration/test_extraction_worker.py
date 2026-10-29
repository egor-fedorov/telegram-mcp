"""Real disposable workers are bounded, cancelled and reaped on every exit path."""

import asyncio
import sys

import pytest

from telegram_mcp.adapters.extraction import process as worker
from telegram_mcp.domain.models import ToolError
from tests.support.extraction import docx_bytes, pdf_bytes

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("kind", ["pdf", "docx"])
async def test_real_worker_parses_selected_bytes(kind: str) -> None:
    parser = worker.DocumentParser()
    assert parser.available
    data = pdf_bytes(["First", "Last"]) if kind == "pdf" else docx_bytes()
    try:
        result = await parser.extract(data)
        assert result.format == kind and result.text
        assert not parser._processes
    finally:
        await parser.close()


@pytest.mark.parametrize("data", [b"%PDF-1.7\ninvalid", b"PK\x03\x04invalid"])
async def test_damaged_file_errors_are_sanitized(data: bytes) -> None:
    parser = worker.DocumentParser()
    try:
        with pytest.raises(ToolError, match="DOCUMENT_INVALID") as caught:
            await parser.extract(data)
        assert "invalid" not in caught.value.detail
    finally:
        await parser.close()


def replace_worker(monkeypatch, script: str):
    original = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()

    async def spawn(*args, **kwargs):
        process = await original(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return processes, started


@pytest.mark.parametrize("stop", ["cancel", "timeout", "close"])
async def test_stalled_worker_is_killed_and_reaped(monkeypatch, stop: str) -> None:
    processes, started = replace_worker(
        monkeypatch, "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"
    )
    parser = worker.DocumentParser()
    if stop == "timeout":
        monkeypatch.setattr(worker, "PARSER_TIMEOUT_SECONDS", 0.2)
    task = asyncio.create_task(parser.extract(b"input"))
    await started.wait()
    if stop == "cancel":
        task.cancel()
        expected = asyncio.CancelledError
    elif stop == "close":
        await parser.close()
        expected = ToolError
    else:
        expected = ToolError
    with pytest.raises(expected):
        await task
    assert not parser._processes and all(p.returncode is not None for p in processes)
    await parser.close()


async def test_unbounded_worker_output_is_stopped_before_decoding(monkeypatch) -> None:
    monkeypatch.setattr(worker, "MAX_RESULT_BYTES", 128)
    processes, _ = replace_worker(
        monkeypatch, "import sys; sys.stdin.buffer.read(); sys.stdout.write('x'*10000)"
    )
    parser = worker.DocumentParser()
    with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
        await parser.extract(b"input")
    assert all(p.returncode is not None for p in processes)


async def test_worker_does_not_inherit_credentials(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_HASH", "private-canary")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "another-canary")
    script = (
        "import os,sys;sys.stdin.buffer.read();"
        "assert 'TELEGRAM_API_HASH' not in os.environ;"
        "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ;"
        'print(\'{"format":"docx","text":"safe"}\')'
    )
    replace_worker(monkeypatch, script)
    assert (await worker.DocumentParser().extract(b"input")).text == "safe"


async def test_cancellation_during_process_creation_reaps_the_child(monkeypatch) -> None:
    original = asyncio.create_subprocess_exec
    started, resume = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed(*args, **kwargs):
        child = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(child)
        started.set()
        await resume.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    parser = worker.DocumentParser()
    task = asyncio.create_task(parser.extract(b"input"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(child.returncode is not None for child in processes)
    assert not parser._processes
