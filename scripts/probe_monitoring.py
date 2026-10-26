"""Bounded selected-peer metadata checks without a dialog catalog or history sweep."""

from scripts.probe_artifacts import ProbeRecorder
from telegram_mcp.application.change_ports import IncrementalPort
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import ToolError


async def check_monitoring(
    service: Service, backend: IncrementalPort, chat_ids: list[str], recorder: ProbeRecorder
) -> None:
    async def metadata() -> int:
        states = await backend.peer_states(chat_ids)
        if set(states) - set(chat_ids):
            raise ToolError("VALIDATION_SCOPE_DENIED", "Peer metadata escaped the selected scope.")
        return len(states)

    for number in range(2):
        count = await recorder.call(f"selected_peer_batch_{number}", metadata())
        if count is not None and count < len(chat_ids):
            recorder.add(f"selected_peer_coverage_{number}", count, skipped=True)
    # Resolution of the explicitly supplied usernames was already measured by
    # setup. Create bounded watches by their pinned IDs, without a second round
    # of username resolution or any message-history warmup.
    for offset in range(0, len(chat_ids), 20):
        name = f"probe-{offset // 20}"
        first = await service.create_watch(name, chat_refs=chat_ids[offset : offset + 20])
        retry = await service.create_watch(name, chat_refs=chat_ids[offset : offset + 20])
        if first != retry:
            raise ToolError(
                "VALIDATION_MISMATCH", "Idempotent watch creation changed its position."
            )
        recorder.add(f"watch_creation_{offset // 20}", first.source_count)
        await service.delete_watch(name)
