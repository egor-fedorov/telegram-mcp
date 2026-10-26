"""Local catalog commands retain the backend while incremental loading continues."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call

import pytest

from telegram_mcp.adapters.cli import CliDependencies, configure, diagnose, load_catalog
from telegram_mcp.adapters.config import Settings
from telegram_mcp.domain.models import Chat, Folder, ToolError
from tests.support.fakes import FakeBackend


def dependencies(backend: FakeBackend) -> CliDependencies:
    return CliDependencies(Mock(return_value=backend), Mock(), AsyncMock(), Mock(), AsyncMock())


async def test_catalog_command_drains_progress_and_keeps_stdout_valid_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = FakeBackend()
    rows = [Chat(id="1", title="Example", type="private")]
    backend.catalog = AsyncMock(
        side_effect=[
            ToolError("CATALOG_LOADING", "500 dialogs loaded", 1),
            ToolError("CATALOG_LOADING", "1000 dialogs loaded", 2),
            (rows, []),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("telegram_mcp.adapters.cli.asyncio.sleep", sleep)
    deps = dependencies(backend)
    await diagnose(Settings(), deps, catalog=True)
    output = capsys.readouterr()
    assert json.loads(output.out)["chats"][0]["id"] == "1"
    assert "500 dialogs" in output.err and "1000 dialogs" in output.err
    assert sleep.await_args_list == [call(1), call(2)]
    assert backend.catalog.await_count == 3 and backend.closed
    assert isinstance(deps.backend_factory, Mock)
    deps.backend_factory.assert_called_once()


async def test_configure_finishes_loading_before_displaying_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    backend.catalog = AsyncMock(
        side_effect=[
            ToolError("CATALOG_LOADING", "Catalog refresh in progress", 1),
            ([], [Folder(id=1, title="Ready", position=0)]),
        ]
    )
    monkeypatch.setattr("telegram_mcp.adapters.cli.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", Mock(return_value=""))
    path = tmp_path / "config.toml"
    await configure(Settings(), path, lambda _: backend)
    assert backend.catalog.await_count == 2 and backend.closed and path.exists()


@pytest.mark.parametrize("code", ["RATE_LIMITED", "CONNECTION_ERROR", "AUTH_REQUIRED"])
async def test_local_catalog_does_not_retry_other_failures(
    code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    backend.catalog = AsyncMock(side_effect=ToolError(code, "Try again later", 17))
    sleep = AsyncMock()
    monkeypatch.setattr("telegram_mcp.adapters.cli.asyncio.sleep", sleep)
    with pytest.raises(ToolError, match=code):
        await diagnose(Settings(), dependencies(backend), catalog=True)
    backend.catalog.assert_awaited_once()
    sleep.assert_not_awaited()
    assert backend.closed


async def test_local_catalog_timeout_is_bounded_and_actionable() -> None:
    backend = FakeBackend()
    cancelled = asyncio.Event()

    async def blocked_catalog():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    backend.catalog = AsyncMock(side_effect=blocked_catalog)
    with pytest.raises(ToolError, match="CATALOG_TIMEOUT") as caught:
        await load_catalog(backend, timeout_seconds=0.001)
    assert cancelled.is_set() and "running MCP server" in caught.value.detail


async def test_cancelled_catalog_command_closes_backend() -> None:
    backend = FakeBackend()
    entered = asyncio.Event()

    async def blocked_catalog():
        entered.set()
        await asyncio.Event().wait()

    backend.catalog = AsyncMock(side_effect=blocked_catalog)
    pending = asyncio.create_task(diagnose(Settings(), dependencies(backend), catalog=True))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert backend.closed
