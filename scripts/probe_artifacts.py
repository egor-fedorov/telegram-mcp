"""Optional M3/M4 checks; persist artifacts only inside the probe's disposable root."""

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.application.ports import Clock
from telegram_mcp.application.service import Service
from telegram_mcp.domain.changes import ExportHistoryResult
from telegram_mcp.domain.models import ToolError


class ProbeRecorder(Protocol):
    def add(
        self, name: str, count: int = 0, codes: list[str] | None = None, *, skipped: bool = False
    ) -> None: ...
    async def call(self, name: str, operation: Any) -> Any: ...


def verify_bytes(data: bytes, size: int, digest: str) -> None:
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ToolError("VALIDATION_MISMATCH", "Artifact size or checksum differs.")


async def check_attachment(service: Service, attachment_id: str) -> int:
    result = await service.download_attachment(attachment_id, save=True)
    data, mime = await service.read_download_resource(result.resource_uri)
    verify_bytes(data, result.size, result.sha256)
    if mime != result.mime_type or result.saved_file is None:
        raise ToolError("VALIDATION_MISMATCH", "Artifact delivery metadata differs.")
    saved = await asyncio.to_thread(Path(result.saved_file.local_path).read_bytes)
    verify_bytes(saved, result.size, result.sha256)
    metadata = json.loads(
        await asyncio.to_thread(Path(result.saved_file.metadata_path).read_text, encoding="utf-8")
    )
    if (
        metadata["source"]["message_ref"] != result.message_ref.model_dump()
        or metadata["sha256"] != result.sha256
    ):
        raise ToolError("VALIDATION_MISMATCH", "Archive source metadata differs.")
    return result.size


async def check_export(service: Service, result: ExportHistoryResult) -> int:
    if result.export is None:
        raise ToolError("VALIDATION_MISMATCH", "No export artifact was produced.")
    exported = result.export
    text, mime = await service.read_export_resource(exported.resource_uri)
    verify_bytes(text.encode("utf-8"), exported.size, exported.sha256)
    if mime != exported.mime_type or exported.saved_file is None:
        raise ToolError("VALIDATION_MISMATCH", "Export delivery metadata differs.")
    saved = await asyncio.to_thread(Path(exported.saved_file.local_path).read_bytes)
    verify_bytes(saved, exported.size, exported.sha256)
    metadata = json.loads(
        await asyncio.to_thread(Path(exported.saved_file.metadata_path).read_text, encoding="utf-8")
    )
    if metadata["source"]["chat_id"] != result.chat_id or metadata["sha256"] != exported.sha256:
        raise ToolError("VALIDATION_MISMATCH", "Archive source metadata differs.")
    if exported.format == "jsonl":
        rows = [json.loads(line) for line in text.splitlines()]
        if not rows or rows[0].get("record") != "manifest" or rows[-1].get("record") != "summary":
            raise ToolError("VALIDATION_MISMATCH", "Export framing differs.")
    return result.exported_count


async def check_changes_and_exports(
    service: Service,
    recorder: ProbeRecorder,
    chat_ids: list[str],
    session_path: Path,
    checkpoint_root: Path,
    clock: Clock,
    binding: str,
) -> None:
    for index, cid in enumerate(chat_ids):
        baseline = await recorder.call(
            f"changes_baseline_{index}", service.get_changes(chat_ref=cid)
        )
        if baseline is not None and baseline.next_checkpoint:
            reopened = CheckpointStore(session_path, clock, root=checkpoint_root)
            try:
                snapshot = reopened.get(baseline.next_checkpoint, binding, "changes")
                if snapshot.chat_id != cid:
                    raise ToolError("VALIDATION_MISMATCH", "Reopened checkpoint source differs.")
                recorder.add(f"checkpoint_reopen_{index}", 1)
            finally:
                reopened.close()
            checkpoint = baseline.next_checkpoint
            # One RPC per tool call: head lookup, then bounded recheck/data.
            for page in range(2):
                result = await recorder.call(
                    f"changes_continue_{index}_{page}",
                    service.get_changes(checkpoint=checkpoint, limit=5),
                )
                if result is None or result.complete or not result.next_checkpoint:
                    break
                checkpoint = result.next_checkpoint
        checkpoint = None
        # A one-day range, two parts, and at most five fetched export records.
        for page in range(2):
            result = await recorder.call(
                f"export_{index}_{page}",
                service.export_history(
                    chat_ref=cid if checkpoint is None else None,
                    date_from=clock.utc_now() - timedelta(days=1) if checkpoint is None else None,
                    checkpoint=checkpoint,
                    save=True,
                ),
            )
            if result is None:
                break
            await recorder.call(f"export_resource_{index}_{page}", check_export(service, result))
            if result.complete or not result.next_checkpoint:
                break
            checkpoint = result.next_checkpoint
