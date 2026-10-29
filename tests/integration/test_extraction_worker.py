"""Real disposable workers are bounded, cancelled and reaped on every exit path."""

import asyncio
import sys
from pathlib import Path

import pytest

from telegram_mcp.adapters.extraction import process as worker
from telegram_mcp.adapters.extraction.limits import MEMORY_LIMIT_EXIT_CODE
from telegram_mcp.domain.models import ToolError
from tests.support.extraction import damaged_pdf_bytes, docx_bytes, pdf_bytes

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


def replace_worker(monkeypatch, script: str, *, spawn_delay: float = 0):
    original = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()

    async def spawn(*args, **kwargs):
        process = await original(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        started.set()
        if spawn_delay:
            await asyncio.sleep(spawn_delay)
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


async def test_full_worker_pipe_is_drained_and_reaped_after_output_limit(monkeypatch) -> None:
    monkeypatch.setattr(worker, "MAX_RESULT_BYTES", 128)
    processes, _ = replace_worker(
        monkeypatch,
        "import sys; sys.stdout.buffer.write(b'x' * (32 * 1024 * 1024)); sys.stdout.flush()",
        spawn_delay=0.2,
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


@pytest.mark.parametrize("consume_input", [False, True])
async def test_memory_exit_is_preserved_even_when_stdin_breaks(monkeypatch, consume_input: bool):
    script = "import os,sys;"
    if consume_input:
        script += "sys.stdin.buffer.read();"
    script += f"os._exit({MEMORY_LIMIT_EXIT_CODE})"
    parser = worker.DocumentParser()
    with monkeypatch.context() as context:
        processes, _ = replace_worker(context, script)
        with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
            await parser.extract(b"x" * (2 * 1024 * 1024))
        assert not parser._processes and all(
            p.returncode == MEMORY_LIMIT_EXIT_CODE for p in processes
        )
    try:
        assert (await parser.extract(docx_bytes("After failure"))).text == "After failure\n"
    finally:
        await parser.close()


@pytest.mark.parametrize("stage", ["startup", "parse", "serialize", "error", "output"])
async def test_worker_memory_errors_use_the_dedicated_exit(monkeypatch, stage: str):
    script = f"""
import sys
from telegram_mcp.adapters.extraction import worker, parsers
def fail(*args, **kwargs):
    raise MemoryError('private-canary')
stage = {stage!r}
if stage == 'startup':
    worker.limit_memory = fail
elif stage == 'parse':
    parsers.extract_document = fail
elif stage == 'serialize':
    class Document:
        model_dump_json = fail
    parsers.extract_document = lambda _: Document()
elif stage == 'error':
    worker._error = fail
else:
    class Output:
        buffer = None
        write = fail
    output = Output()
    output.buffer = output
    sys.stdout = output
worker.main()
"""
    processes, _ = replace_worker(monkeypatch, script)
    parser = worker.DocumentParser()
    try:
        with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED") as caught:
            await parser.extract(b"unsupported" if stage == "error" else pdf_bytes(["Small"]))
        assert "private-canary" not in caught.value.detail
        assert all(p.returncode == MEMORY_LIMIT_EXIT_CODE for p in processes)
    finally:
        await parser.close()


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux only")
async def test_real_linux_limit_rejects_allocation_without_large_memory_use(monkeypatch):
    script = """
import resource
from telegram_mcp.adapters.extraction import limits, worker, parsers
limits.MAX_WORKER_MEMORY_BYTES = 128 * 1024 * 1024
def allocate(data):
    assert resource.getrlimit(resource.RLIMIT_AS) == (128 * 1024 * 1024,) * 2
    bytearray(128 * 1024 * 1024)
    raise AssertionError('Allocation should exceed address space including the interpreter')
parsers.extract_document = allocate
worker.main()
"""
    processes, _ = replace_worker(monkeypatch, script)
    parser = worker.DocumentParser()
    try:
        with pytest.raises(ToolError, match="EXTRACTION_LIMIT_EXCEEDED"):
            await parser.extract(b"small")
        assert all(p.returncode == MEMORY_LIMIT_EXIT_CODE for p in processes)
    finally:
        await parser.close()


@pytest.mark.parametrize("returncode", [1, 7, -9])
async def test_unrelated_worker_exit_is_not_misclassified(monkeypatch, returncode: int):
    script = (
        f"import os; os._exit({returncode})"
        if returncode > 0
        else "import os; os.kill(os.getpid(), 9)"
    )
    replace_worker(monkeypatch, script)
    parser = worker.DocumentParser()
    try:
        with pytest.raises(ToolError, match="EXTRACTION_FAILED"):
            await parser.extract(b"x" * (2 * 1024 * 1024))
    finally:
        await parser.close()


async def test_memory_limit_setup_failure_never_parses_input(monkeypatch):
    script = """
from telegram_mcp.adapters.extraction import worker, parsers
from telegram_mcp.domain.models import ToolError
def fail():
    raise ToolError('EXTRACTION_FAILED', 'Cannot apply the document worker memory limit.')
def unexpected_parse(data):
    raise AssertionError('Parsing must not run without the required memory limit')
worker.limit_memory = fail
parsers.extract_document = unexpected_parse
worker.main()
"""
    processes, _ = replace_worker(monkeypatch, script)
    parser = worker.DocumentParser()
    try:
        with pytest.raises(ToolError, match="Cannot apply the document worker memory limit"):
            await parser.extract(b"x" * (2 * 1024 * 1024))
        assert all(p.returncode == 0 for p in processes) and not parser._processes
    finally:
        await parser.close()


async def test_pdf_retry_shares_one_worker_deadline(monkeypatch, tmp_path: Path):
    marker = tmp_path / "fallback-started"
    script = f"""
import time
from pathlib import Path
from pypdf.errors import PdfReadError
from telegram_mcp.adapters.extraction import worker, parsers
def read(data, *, strict):
    if strict:
        time.sleep(0.1)
        raise PdfReadError('Recoverable')
    Path({str(marker)!r}).write_text('started')
    time.sleep(60)
parsers._read_pdf = read
worker.main()
"""
    monkeypatch.setattr(worker, "PARSER_TIMEOUT_SECONDS", 5)
    processes, _ = replace_worker(monkeypatch, script)
    parser = worker.DocumentParser()
    try:
        with pytest.raises(ToolError, match="EXTRACTION_TIMEOUT"):
            await parser.extract(damaged_pdf_bytes("length"))
        assert len(processes) == 1 and processes[0].returncode is not None
        assert marker.read_text() == "started"
    finally:
        await parser.close()
