"""Authenticated shared serving and real MCP forwarding without Telegram credentials."""

import asyncio
import json
import socket
import stat
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent
from starlette.applications import Starlette

from telegram_mcp.adapters import http_token
from telegram_mcp.adapters.bridge import connect, create_bridge, shared_client
from telegram_mcp.adapters.config import Settings, save_settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.tool_gate import ToolCallGate
from telegram_mcp.adapters.transport import _serve_http, create_http_app, endpoint
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import ToolError
from tests.fakes import FakeBackend, msg


@asynccontextmanager
async def running_server(tmp_path: Path) -> AsyncIterator[tuple[Settings, FakeBackend]]:
    backend = FakeBackend([msg("1", 1), msg("1", 2)])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        settings = Settings(
            allowed_chat_ids=["1"],
            http_port=listener.getsockname()[1],
            http_token_path=tmp_path / "mcp.token",
        )
        app = create_http_app(
            build_service(backend, settings),
            load_token(settings.http_token_path, create=True),
            settings.http_port,
        )
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield settings, backend
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


def test_http_token_persists_and_has_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "private" / "token"
    first = load_token(path, create=True)
    assert len(first) == 43
    assert load_token(path, create=True) == load_token(path) == first
    if sys.platform == "win32":
        import win32security

        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl.GetAceCount() == 1
        assert descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o644)
        assert load_token(path, create=True) == first
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("contents", [None, "", "synthetic-secret", "a" * 1025, "non-ascii: é"])
def test_invalid_or_missing_token_is_redacted(tmp_path: Path, contents: str | None) -> None:
    path = tmp_path / "token"
    if contents is not None:
        path.write_text(contents)
    with pytest.raises(ToolError, match="HTTP_TOKEN_ERROR") as caught:
        load_token(path)
    assert contents is None or not contents or contents not in str(caught.value)
    assert "serve --transport http" in str(caught.value)


def test_token_permission_failure_removes_empty_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_path: Path) -> None:
        raise PermissionError("synthetic-private-path")

    monkeypatch.setattr(http_token, "protect_token", denied)
    path = tmp_path / "token"
    with pytest.raises(ToolError, match="HTTP_TOKEN_ERROR") as caught:
        load_token(path, create=True)
    assert not path.exists()
    assert "synthetic-private-path" not in str(caught.value)


async def test_http_requires_auth_for_every_method_and_rejects_foreign_origins(
    tmp_path: Path,
) -> None:
    async with running_server(tmp_path) as (settings, backend):
        token = load_token(settings.http_token_path)
        async with httpx2.AsyncClient(trust_env=False) as http:
            for method in ("POST", "GET", "DELETE", "OPTIONS"):
                response = await http.request(method, endpoint(settings))
                assert response.status_code == 401
                assert token not in response.text
            for authorization in ("Bearer wrong-secret", f"Basic {token}", "Bearer"):
                response = await http.post(
                    endpoint(settings), headers={"Authorization": authorization}
                )
                assert response.status_code == 401
                assert token not in response.text
                assert "wrong-secret" not in response.text
            for extra, expected in [
                ({"Origin": "https://untrusted.example"}, 403),
                ({"Host": "untrusted.example"}, 421),
            ]:
                response = await http.post(
                    endpoint(settings),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        **extra,
                    },
                    json={},
                )
                assert response.status_code == expected
            duplicate = await http.post(
                endpoint(settings),
                headers=[("Authorization", f"Bearer {token}")] * 2,
            )
            assert duplicate.status_code == 401
            assert backend.backend_calls == []
    assert backend.backend_calls == ["close"]


async def test_default_http_port_accepts_normalized_host_and_origin() -> None:
    backend = FakeBackend()
    token = "a" * 43
    app = create_http_app(build_service(backend, Settings()), token, 80)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:80",
            headers={"Authorization": f"Bearer {token}", "Origin": "http://127.0.0.1"},
        ) as client:
            response = await client.post("/mcp", json={})
            assert response.status_code not in (401, 403, 421)
            response = await client.post("/mcp", json={}, headers={"Host": "127.0.0.1:8080"})
            assert response.status_code == 421


@pytest.mark.parametrize("first_mode,second_mode", [("auto", "legacy"), ("legacy", "auto")])
async def test_two_http_clients_share_cursors_and_only_daemon_owns_shutdown(
    tmp_path: Path, first_mode: str, second_mode: str
) -> None:
    async with running_server(tmp_path) as (settings, backend):
        token = load_token(settings.http_token_path)
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, trust_env=False
        ) as http:
            async with Client(
                streamable_http_client(endpoint(settings), http_client=http), mode=first_mode
            ) as first:
                async with Client(
                    streamable_http_client(endpoint(settings), http_client=http), mode=second_mode
                ) as second:
                    one, two = await asyncio.gather(first.list_tools(), second.list_tools())
                    assert len(one.tools) == 14 and one.tools == two.tools
                    page = await first.call_tool("get_history", {"chat_id": "1", "limit": 1})
                    assert page.structured_content is not None
                    cursor = page.structured_content["next_cursor"]
                    assert cursor
                    continued = await second.call_tool(
                        "get_history", {"chat_id": "1", "limit": 1, "cursor": cursor}
                    )
                    assert continued.structured_content is not None
                    assert (
                        continued.structured_content["messages"][0]["message_ref"]["message_id"]
                        == 1
                    )
                    settings.allowed_chat_ids.clear()
                    denied = await first.call_tool(
                        "get_history", {"chat_id": "1", "limit": 1, "cursor": cursor}
                    )
                    assert denied.is_error and isinstance(denied.content[0], TextContent)
                    assert "ACCESS_DENIED" in denied.content[0].text
                assert not backend.closed
                assert not (await first.call_tool("get_status", {})).is_error
            assert not backend.closed
    assert backend.backend_calls.count("close") == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_bridge_forwards_schemas_instructions_and_error_results(
    tmp_path: Path, mode: str
) -> None:
    async with running_server(tmp_path) as (settings, backend):
        async with shared_client(settings) as upstream:
            direct_tools = await upstream.list_tools()
            proxy = create_bridge(upstream)
            async with Client(proxy, mode=mode) as client:
                assert client.instructions == upstream.instructions
                assert (await client.list_tools()).tools == direct_tools.tools
                for arguments in (
                    {"chat_id": "1"},
                    {"chat_id": "99"},
                    {"chat_id": "1", "limit": 101},
                ):
                    direct = await upstream.call_tool("get_history", arguments)
                    proxied = await client.call_tool("get_history", arguments)
                    assert proxied.is_error == direct.is_error
                    if direct.is_error:
                        assert proxied.content == direct.content
                    else:
                        assert proxied.structured_content is not None
                        assert direct.structured_content is not None
                        assert (
                            proxied.structured_content["messages"]
                            == direct.structured_content["messages"]
                        )
            assert not backend.closed
        assert not backend.closed


async def test_two_stdio_bridge_subprocesses_keep_daemon_alive(tmp_path: Path) -> None:
    async with running_server(tmp_path) as (settings, backend):
        path = tmp_path / "config.toml"
        save_settings(settings, path)
        command = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telegram_mcp.cli", "--config", str(path), "connect"],
        )
        async with Client(command, mode="legacy") as first:
            async with Client(command, mode="auto") as second:
                results = await asyncio.gather(first.list_tools(), second.list_tools())
                assert len(results[0].tools) == len(results[1].tools) == 14
                page = await first.call_tool("get_messages", {"refs": ["1:1"]})
                assert not page.is_error
                assert not (await second.call_tool("get_status", {})).is_error
            assert not backend.closed
            assert not (await first.call_tool("get_status", {})).is_error
        assert not backend.closed
    assert backend.backend_calls.count("close") == 1


async def test_missing_daemon_fails_without_creating_telegram_session(tmp_path: Path) -> None:
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        settings = Settings(
            http_port=unavailable.getsockname()[1],
            http_token_path=tmp_path / "token",
            session_path=tmp_path / "must-not-exist.session",
        )
        token = load_token(settings.http_token_path, create=True)
        with pytest.raises(ToolError, match="SHARED_SERVER_UNAVAILABLE") as caught:
            await connect(settings)
        assert token not in str(caught.value)
        assert not settings.session_path.exists()
        lock_path = Path(str(settings.session_path) + ".lock")
        assert not await asyncio.to_thread(lock_path.exists)


async def test_wrong_token_has_safe_actionable_error_and_cannot_reach_backend(
    tmp_path: Path,
) -> None:
    async with running_server(tmp_path) as (settings, backend):
        other = settings.model_copy(update={"http_token_path": tmp_path / "other-token"})
        wrong = load_token(other.http_token_path, create=True)
        with pytest.raises(ToolError, match="SHARED_SERVER_UNAVAILABLE") as caught:
            await connect(other)
        assert "same token file" in str(caught.value)
        assert wrong not in str(caught.value)
        assert not backend.backend_calls


async def test_restart_invalidates_old_cursor_without_reading_messages(tmp_path: Path) -> None:
    async with running_server(tmp_path) as (settings, _backend):
        async with shared_client(settings) as client:
            page = await client.call_tool("get_history", {"chat_id": "1", "limit": 1})
            assert page.structured_content is not None
            cursor = page.structured_content["next_cursor"]
    async with running_server(tmp_path) as (settings, backend):
        async with shared_client(settings) as client:
            page = await client.call_tool(
                "get_history", {"chat_id": "1", "limit": 1, "cursor": cursor}
            )
            assert page.is_error and isinstance(page.content[0], TextContent)
            assert "CURSOR_EXPIRED" in page.content[0].text
            assert not backend.fetches


async def test_bridge_exits_on_daemon_shutdown_without_replaying_a_call(tmp_path: Path) -> None:
    async with AsyncExitStack() as daemon:
        settings, backend = await daemon.enter_async_context(running_server(tmp_path))
        with pytest.raises(ToolError, match="SHARED_SERVER_UNAVAILABLE") as caught:
            async with shared_client(settings) as upstream:
                async with Client(create_bridge(upstream)) as client:
                    await daemon.aclose()
                    await client.call_tool("get_status", {})
        assert load_token(settings.http_token_path) not in str(caught.value)
        assert backend.backend_calls == ["close"]


async def test_http_serializes_entire_tools_but_does_not_block_tool_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_server(tmp_path) as (settings, backend):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        original = backend.status

        async def blocked_status() -> dict[str, object]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await original()

        monkeypatch.setattr(backend, "status", blocked_status)
        async with shared_client(settings) as first, shared_client(settings) as second:
            active = asyncio.create_task(first.call_tool("get_status", {}))
            await entered.wait()
            queued = asyncio.create_task(second.call_tool("get_status", {}))
            assert len((await second.list_tools()).tools) == 14
            await asyncio.sleep(0.02)
            assert calls == 1
            release.set()
            results = await asyncio.gather(active, queued)
            assert not any(result.is_error for result in results)
            assert calls == 2


async def test_occupied_http_port_has_actionable_error(tmp_path: Path) -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        settings = Settings(http_port=occupied.getsockname()[1], http_token_path=tmp_path / "token")
        backend = FakeBackend()
        with pytest.raises(ToolError, match="HTTP_BIND_FAILED"):
            await _serve_http(build_service(backend, settings), settings)
        assert backend.backend_calls == ["close"]


async def test_token_startup_failure_closes_the_service_once(tmp_path: Path) -> None:
    settings = Settings(http_token_path=tmp_path / "token")
    await asyncio.to_thread(settings.http_token_path.write_text, "malformed")
    backend = FakeBackend()
    with pytest.raises(ToolError, match="HTTP_TOKEN_ERROR"):
        await _serve_http(build_service(backend, settings), settings)
    assert backend.backend_calls == ["close"]


@pytest.mark.parametrize("fail", [False, True])
async def test_http_runner_owns_service_close_even_when_asgi_startup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    async def serve(server: uvicorn.Server, sockets=None) -> None:
        if fail:
            raise RuntimeError("synthetic startup failure")
        assert isinstance(server.config.app, Starlette)
        async with server.config.app.router.lifespan_context(server.config.app):
            pass

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    settings = Settings(http_token_path=tmp_path / "token").model_copy(update={"http_port": 0})
    backend = FakeBackend()
    if fail:
        with pytest.raises(RuntimeError, match="synthetic startup failure"):
            await _serve_http(build_service(backend, settings), settings)
    else:
        await _serve_http(build_service(backend, settings), settings)
    assert backend.backend_calls == ["close"]


async def test_gate_busy_result_and_cancellation_do_not_block_later_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = ToolCallGate(timeout_seconds=0.01)
    backend = FakeBackend()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = backend.status

    async def blocked_status() -> dict[str, object]:
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(backend, "status", blocked_status)
    server = create_server(build_service(backend, Settings()), gate=gate)
    async with Client(server) as client:
        first = asyncio.create_task(client.call_tool("get_status", {}))
        await entered.wait()
        busy = await client.call_tool("get_status", {})
        assert busy.is_error
        assert isinstance(busy.content[0], TextContent)
        text = busy.content[0].text
        issue = json.loads(text[text.index("{") :])
        assert issue["code"] == "SERVER_BUSY" and issue["retry_after_seconds"] == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert not (await client.call_tool("get_status", {})).is_error
        assert backend.backend_calls == ["status"]


async def test_cancelled_gate_waiter_does_not_execute_or_release_active_call() -> None:
    gate = ToolCallGate()
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def active():
        entered.set()
        await release.wait()
        calls.append("active")

    async def queued():
        calls.append("queued")

    first = asyncio.create_task(gate.run(active))
    await entered.wait()
    waiting = asyncio.create_task(gate.run(queued))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not calls
    release.set()
    await first
    await gate.run(queued)
    assert calls == ["active", "queued"]
