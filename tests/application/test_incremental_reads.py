"""A forward read that cannot advance reports a retryable issue and keeps its position."""

from telegram_mcp.application.change_ports import ObservationBatch
from tests.support.fakes import msg
from tests.support.incremental import incremental_fixture


async def stalled(chat_id, *, after_id, through_id, limit, date_from=None) -> ObservationBatch:
    return ObservationBatch(observations=[], next_offset=after_id, exhausted=False)


async def test_stalled_change_read_keeps_the_previous_position(tmp_path, monkeypatch):
    async with incremental_fixture(tmp_path) as env:
        baseline = await env.service.get_changes(chat_ref="1")
        assert baseline.next_checkpoint
        env.backend.stored_messages.append(msg("1", 4))
        monkeypatch.setattr(env.backend, "read_incremental", stalled)
        page = await env.service.get_changes(checkpoint=baseline.next_checkpoint)
        assert [(i.code, i.message) for i in page.issues] == [
            ("TELEGRAM_UNAVAILABLE", "History did not advance; retry this position.")
        ]
        assert not page.complete and page.changes == []


async def test_stalled_export_keeps_a_resumable_checkpoint(tmp_path, monkeypatch):
    async with incremental_fixture(tmp_path) as env:
        monkeypatch.setattr(env.backend, "read_incremental", stalled)
        part = await env.service.export_history(chat_ref="1")
        assert [(i.code, i.message) for i in part.issues] == [
            ("TELEGRAM_UNAVAILABLE", "History did not advance; retry this checkpoint.")
        ]
        assert not part.complete and part.next_checkpoint and part.exported_count == 0
