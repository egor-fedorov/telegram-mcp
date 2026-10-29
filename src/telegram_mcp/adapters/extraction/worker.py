"""Private subprocess protocol: bounded document bytes in, bounded JSON out."""

import json
import logging
import os
import sys

from telegram_mcp.adapters.extraction.limits import MEMORY_LIMIT_EXIT_CODE, limit_memory
from telegram_mcp.application.attachment_ports import MAX_ATTACHMENT_BYTES
from telegram_mcp.application.extraction_ports import MAX_RESULT_BYTES
from telegram_mcp.domain.models import ToolError


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        sys.stdout.buffer.write(_parse_input())
        sys.stdout.buffer.flush()
    except MemoryError:
        # Avoid allocating JSON or running interpreter cleanup while out of memory.
        os._exit(MEMORY_LIMIT_EXIT_CODE)


def _parse_input() -> bytes:
    try:
        limit_memory()
        from telegram_mcp.adapters.extraction.parsers import extract_document

        data = sys.stdin.buffer.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ToolError("ATTACHMENT_TOO_LARGE", "Attachments are limited to 20 MiB.")
        result = extract_document(data).model_dump_json().encode("utf-8")
        if len(result) > MAX_RESULT_BYTES:
            raise ToolError("EXTRACTION_LIMIT_EXCEEDED", "The extracted result exceeds 8 MiB.")
    except ImportError:
        result = _error("EXTRACTION_UNAVAILABLE", "Install telegram-context-mcp[extraction].")
    except ToolError as exc:
        result = _error(exc.code, exc.detail)
    except MemoryError:
        raise
    except Exception:
        result = _error("EXTRACTION_FAILED", "The document worker failed unexpectedly.")
    return result


def _error(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")


if __name__ == "__main__":
    main()
