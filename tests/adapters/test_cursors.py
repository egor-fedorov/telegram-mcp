import pytest

from telegram_mcp.adapters.cursors import CursorStore
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import FakeClock


def test_cursor_snapshot_expiry_and_memory_limit() -> None:
    clock = FakeClock()
    store = CursorStore(clock, ttl=10, maximum=1, max_bytes=1000)
    value = ["initial"]
    token = store.put("test", "fp", value)
    value.append("changed")
    assert store.get(token, "test", "fp") == ["initial"]
    store.put("test", "fp", [])
    with pytest.raises(ToolError, match="CURSOR_EXPIRED"):
        store.get(token, "test", "fp")
    with pytest.raises(ToolError, match="QUERY_TOO_LARGE"):
        store.put("test", "fp", "x" * 2000)
    token = store.put("test", "fp", [])
    clock.time = float("inf")
    with pytest.raises(ToolError, match="CURSOR_EXPIRED"):
        store.get(token, "test", "fp")
