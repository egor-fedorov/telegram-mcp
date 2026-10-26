import asyncio
from dataclasses import replace

import pytest

from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, ToolError
from tests.support.fakes import FakeBackend
from tests.support.incremental import incremental_fixture


async def test_create_rejects_large_selector_before_network_and_deduplicates_aliases(tmp_path):
    backend = FakeBackend(
        chats=[Chat(id="1", title="Selected", username="selected", type="channel")]
    )
    async with incremental_fixture(tmp_path, backend=backend) as env:
        with pytest.raises(ToolError, match="1..20"):
            await env.service.create_watch("jobs", chat_refs=["@selected"] * 21)
        assert not backend.backend_calls
        result = await env.service.create_watch(
            "jobs", chat_refs=["@SELECTED", "https://t.me/s/selected", "selected"]
        )
        assert result.source_count == 1
        assert backend.resolve_calls == [("selected", True)]


async def test_create_deadline_and_floodwait_leave_no_partial_watch(tmp_path, monkeypatch):
    async with incremental_fixture(tmp_path, settings=Settings(allowed_chat_ids=["1", "2"])) as env:
        original = env.backend.resolve_chat
        env.service._incremental.limits = replace(
            env.service._incremental.limits, page_deadline_seconds=0
        )

        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
            return await original(*args, **kwargs)

        monkeypatch.setattr(env.backend, "resolve_chat", slow)
        with pytest.raises(ToolError, match="CONNECTION_ERROR"):
            await env.service.create_watch("jobs", chat_refs=["1"])
        assert env.watches.list(env.backend.binding) == []
        env.service._incremental.limits = replace(
            env.service._incremental.limits, page_deadline_seconds=35
        )
        called = []

        async def flood(ref, **kwargs):
            called.append(ref)
            raise ToolError("RATE_LIMITED", "Wait", 60)

        monkeypatch.setattr(env.backend, "resolve_chat", flood)
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await env.service.create_watch("jobs", chat_refs=["1", "2"])
        assert called == ["1"] and env.watches.list(env.backend.binding) == []
        with pytest.raises(ToolError, match="RATE_LIMITED"):
            await env.service.create_watch("another", chat_refs=["1"])
        assert called == ["1"]
