"""Terminate optional parsing without blocking the Telegram event loop."""

import asyncio
import importlib.util
import json
import os
import sys
from contextlib import suppress

from pydantic import ValidationError

from telegram_mcp.application.extraction_ports import MAX_RESULT_BYTES, PARSER_TIMEOUT_SECONDS
from telegram_mcp.domain.extraction import ExtractedDocument
from telegram_mcp.domain.models import ToolError

_ERRORS = {
    "DOCUMENT_UNSUPPORTED",
    "DOCUMENT_INVALID",
    "DOCUMENT_ENCRYPTED",
    "DOCUMENT_NO_TEXT",
    "EXTRACTION_LIMIT_EXCEEDED",
    "EXTRACTION_UNAVAILABLE",
    "EXTRACTION_FAILED",
    "ATTACHMENT_TOO_LARGE",
}


class DocumentParser:
    def __init__(self) -> None:
        self._processes: set[asyncio.subprocess.Process] = set()
        self._closed = False

    @property
    def available(self) -> bool:
        return all(importlib.util.find_spec(name) is not None for name in ("pypdf", "docx"))

    async def extract(self, data: bytes) -> ExtractedDocument:
        if self._closed:
            raise ToolError("EXTRACTION_FAILED", "The document parser has closed.")
        process = await self._spawn()
        self._processes.add(process)
        try:
            if self._closed:
                raise ToolError("EXTRACTION_FAILED", "The document parser has closed.")
            async with asyncio.timeout(PARSER_TIMEOUT_SECONDS):
                payload = await self._exchange(process, data)
            return self._decode(payload)
        except TimeoutError:
            raise ToolError("EXTRACTION_TIMEOUT", "Document parsing exceeded 30 seconds.") from None
        except (OSError, ValueError, ValidationError):
            raise ToolError(
                "EXTRACTION_FAILED", "The document worker did not return a valid result."
            ) from None
        finally:
            await self._stop(process)
            self._processes.discard(process)

    async def _spawn(self) -> asyncio.subprocess.Process:
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "HOME",
                "USERPROFILE",
                "TEMP",
                "TMP",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "LOCALAPPDATA",
            }
        }
        creation = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "telegram_mcp.adapters.extraction.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        )
        try:
            return await asyncio.shield(creation)
        except asyncio.CancelledError:
            while not creation.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(creation)
            with suppress(Exception):
                await self._stop(creation.result())
            raise
        except OSError:
            raise ToolError("EXTRACTION_FAILED", "Cannot start the document worker.") from None

    async def _exchange(self, process: asyncio.subprocess.Process, data: bytes) -> bytes:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(data)
        await process.stdin.drain()
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()
        chunks, size = [], 0
        while chunk := await process.stdout.read(min(65536, MAX_RESULT_BYTES - size + 1)):
            size += len(chunk)
            if size > MAX_RESULT_BYTES:
                raise ToolError(
                    "EXTRACTION_LIMIT_EXCEEDED", "The document worker exceeded 8 MiB of output."
                )
            chunks.append(chunk)
        if await process.wait() != 0:
            raise ToolError("EXTRACTION_FAILED", "The document worker stopped unexpectedly.")
        return b"".join(chunks)

    @staticmethod
    def _decode(payload: bytes) -> ExtractedDocument:
        result = json.loads(payload)
        if isinstance(result, dict) and "error" in result:
            error = result["error"]
            if isinstance(error, dict) and error.get("code") in _ERRORS:
                message = error.get("message")
                if isinstance(message, str) and len(message) <= 1000:
                    raise ToolError(error["code"], message)
            raise ValueError("Invalid worker error")
        return ExtractedDocument.model_validate(result)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process, *, drain: bool = True) -> None:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        # Keep ownership through repeated cancellation until the child is reaped.
        waiter = asyncio.create_task(DocumentParser._reap(process, drain=drain))
        while not waiter.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(waiter)
        waiter.result()

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process, *, drain: bool) -> None:
        # A full stdout reader can pause its pipe and delay Process.wait() even
        # after the child is killed. Drain remaining bytes without accumulating them.
        if drain and process.stdout is not None:
            while await process.stdout.read(65536):
                pass
        await process.wait()

    async def close(self) -> None:
        self._closed = True
        for process in list(self._processes):
            # extract() owns stdout and drains it in its finally block.
            await self._stop(process, drain=False)
