"""Process-local memory enforcement and the worker's allocation-failure exit code."""

import sys

from telegram_mcp.application.extraction_ports import MAX_WORKER_MEMORY_BYTES
from telegram_mcp.domain.models import ToolError

MEMORY_LIMIT_EXIT_CODE = 3


def limit_memory() -> None:
    """Cap Linux address space without relaxing inherited limits or affecting the parent."""
    if sys.platform != "linux":
        return
    try:
        import resource

        inherited = resource.getrlimit(resource.RLIMIT_AS)
        soft, hard = (
            MAX_WORKER_MEMORY_BYTES
            if value == resource.RLIM_INFINITY
            else min(value, MAX_WORKER_MEMORY_BYTES)
            for value in inherited
        )
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
    except (ImportError, OSError, ValueError):
        raise ToolError(
            "EXTRACTION_FAILED", "Cannot apply the document worker memory limit."
        ) from None
