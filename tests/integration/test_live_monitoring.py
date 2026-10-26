import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scripts.probe_telegram import LiveBackend, probe, validate_selectors
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.telegram import TelegramBackend
from telegram_mcp.domain.models import Chat, ToolError
from tests.support.fakes import FakeBackend


class MonitoringBackend(FakeBackend):
    def __init__(self):
        super().__init__(
            chats=[
                Chat(id=f"-100{i}", username=f"selected_{i}", title="private-title", type="channel")
                for i in range(1, 41)
            ]
        )
        self.selected = set()
        self.incremental = False

    def select(self, chat_ids, *, attachment_id=None, m4=False):
        self.selected = chat_ids
        self.incremental = m4

    async def peer_states(self, chat_ids):
        assert self.incremental and set(chat_ids) <= self.selected and len(chat_ids) <= 40
        return await super().peer_states(chat_ids)

    async def disconnect_owned(self):
        pytest.fail("Metadata-only monitoring must not exercise reconnect/history")


async def test_monitoring_probe_bounds_redacts_and_cleans_disposable_state(tmp_path, monkeypatch):
    roots = []

    def directory(**kwargs):
        temporary = tempfile.TemporaryDirectory(dir=tmp_path, **kwargs)
        roots.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr("scripts.probe_telegram.TemporaryDirectory", directory)
    backend = MonitoringBackend()
    checks = await probe(
        Settings(),
        chats=[f"@selected_{i}" for i in range(1, 41)],
        message_refs=[],
        monitoring=True,
        backend_factory=lambda _: backend,
    )
    assert all(check.status == "passed" for check in checks)
    assert [check.count for check in checks if check.check.startswith("selected_peer_batch")] == [
        40,
        40,
    ]
    assert [check.count for check in checks if check.check.startswith("watch_creation")] == [20, 20]
    assert len([ref for ref, _ in backend.resolve_calls if ref.startswith("@")]) == 40
    assert not {
        "catalog",
        "read_incremental",
        "recent_observations",
        "fetch",
        "search_global",
    } & set(backend.backend_calls)
    assert backend.closed and all(not root.exists() for root in roots)
    report = json.dumps([asdict(check) for check in checks])
    assert "private-title" not in report and "selected_1" not in report and "-1001" not in report


async def test_monitoring_probe_stops_at_first_floodwait():
    backend = MonitoringBackend()
    backend.faults["-1001"] = ToolError("RATE_LIMITED", "private details", 30)
    checks = await probe(
        Settings(),
        chats=["@selected_1"],
        message_refs=[],
        monitoring=True,
        backend_factory=lambda _: backend,
    )
    assert checks[-1].issue_codes == ["RATE_LIMITED"]
    assert len(backend.peer_requests) == 1 and backend.closed


async def test_monitoring_opt_in_does_not_expand_normal_probe_or_selected_scope():
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        validate_selectors(["1"] * 4, [], "link")
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        validate_selectors(["1"] * 41, [], "link", monitoring=True)
    with pytest.raises(ToolError, match="INVALID_ARGUMENT"):
        await probe(Settings(), chats=["1"], message_refs=["1:1"], monitoring=True)
    backend = LiveBackend(Settings())
    try:
        backend.select({"1"}, m4=True)
        with patch.object(TelegramBackend, "peer_states", new_callable=AsyncMock) as read:
            with pytest.raises(ToolError, match="VALIDATION_SCOPE_DENIED"):
                await backend.peer_states(["99"])
            with pytest.raises(ToolError, match="VALIDATION_SCOPE_DENIED"):
                await backend.peer_states(["1"] * 41)
            read.assert_not_awaited()
    finally:
        await backend.close()
