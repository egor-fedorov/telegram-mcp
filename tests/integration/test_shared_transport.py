"""Authenticated shared serving and real MCP forwarding without Telegram credentials."""

import asyncio
import socket
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent
from starlette.applications import Starlette

from telegram_mcp.adapters.bridge import connect, create_bridge, shared_client
from telegram_mcp.adapters.config import Settings, save_settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import _serve_http, create_http_app, endpoint
from telegram_mcp.bootstrap import build_service
from telegram_mcp.domain.models import ToolError
from tests.support.fakes import FakeBackend
from tests.support.transport import running_server


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
                    assert len(one.tools) == 17 and one.tools == two.tools
                    page = await first.call_tool("get_history", {"chat_id": "1", "limit": 1})
                    assert page.structured_content is not None
                    cursor = page.structured_content["next_cursor"]
                    assert cursor
                    continued = await second.call_tool(
                        "get_history", {"chat_id": "1", "limit": 1, "cursor": cursor}
                    )
                    assert continued.structured_content is not None
                    assert continued.structured_content["messages"][0]["message_ref"] == "1:1"
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
                assert len(results[0].tools) == len(results[1].tools) == 17
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


async def test_http_overlapping_tools_do_not_block_tool_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_server(tmp_path) as (settings, backend):
        entered, both_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        original = backend.status

        async def blocked_status() -> dict[str, object]:
            nonlocal calls
            calls += 1
            entered.set()
            if calls == 2:
                both_entered.set()
            await release.wait()
            return await original()

        monkeypatch.setattr(backend, "status", blocked_status)
        async with shared_client(settings) as first, shared_client(settings) as second:
            active = asyncio.create_task(first.call_tool("get_status", {}))
            await entered.wait()
            queued = asyncio.create_task(second.call_tool("get_status", {}))
            assert len((await second.list_tools()).tools) == 17
            await asyncio.wait_for(both_entered.wait(), 5)
            assert calls == 2
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
