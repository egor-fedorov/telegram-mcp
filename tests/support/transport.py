import asyncio
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx2
import uvicorn
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import Settings, save_settings
from telegram_mcp.adapters.exports import ExportStore
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import create_http_app, endpoint
from telegram_mcp.adapters.watches import WatchStore
from telegram_mcp.bootstrap import build_service
from tests.support.fakes import FakeBackend, msg
from tests.support.resources import (
    ResourceService,
)


def authorized_http(settings: Settings) -> httpx2.AsyncClient:
    """An HTTP client for MCP sessions; httpx's 5-second default fails on loaded CI runners."""
    return httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {load_token(settings.http_token_path)}"},
        timeout=httpx2.Timeout(30),
        trust_env=False,
    )


@asynccontextmanager
async def running_server(
    tmp_path: Path,
    backend: FakeBackend | None = None,
    *,
    schema_mode: Literal["compact", "full"] = "compact",
    archive: bool = False,
) -> AsyncGenerator[tuple[Settings, FakeBackend]]:
    backend = backend if backend is not None else FakeBackend([msg("1", 1), msg("1", 2)])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        settings = Settings(
            allowed_chat_ids=[chat.id for chat in backend.chats],
            session_path=tmp_path / "account.session",
            http_port=listener.getsockname()[1],
            http_token_path=tmp_path / "mcp.token",
            mcp_schema_mode=schema_mode,
            archive_directory=tmp_path / "archive" if archive else None,
        )
        app = create_http_app(
            build_service(
                backend,
                settings,
                checkpoints=CheckpointStore(
                    settings.session_path, SystemClock(), root=tmp_path / "checkpoints"
                ),
                exports=ExportStore(
                    settings.session_path, SystemClock(), root=tmp_path / "exports"
                ),
                watches=WatchStore(settings.session_path, root=tmp_path / "watches"),
            ),
            load_token(settings.http_token_path, create=True),
            settings.http_port,
            schema_mode=schema_mode,
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


@asynccontextmanager
async def running_resource_server(
    directory: Path, size: int
) -> AsyncGenerator[tuple[Settings, ResourceService]]:
    service = ResourceService(directory, size)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        settings = Settings(
            http_port=listener.getsockname()[1], http_token_path=directory / "resource.token"
        )
        app = create_http_app(
            service.service(),
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
            yield settings, service
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@asynccontextmanager
async def connection(kind, path, mode, schema_mode, settings=None):
    if kind == "stdio":
        script = (
            "from pathlib import Path; import sys; "
            "from tests.support.incremental import export_stdio_service; "
            "from telegram_mcp.adapters.mcp import create_server; "
            "create_server(export_stdio_service(Path(sys.argv[1])), schema_mode=sys.argv[2]).run()"
        )
        parameters = StdioServerParameters(
            command=sys.executable, args=["-c", script, str(path), schema_mode]
        )
        async with Client(parameters, mode=mode, cache=None) as client:
            yield client
    elif kind == "connect":
        assert settings is not None
        config = path / "config.toml"
        save_settings(settings, config)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "telegram_mcp.cli", "--config", str(config), "connect"],
        )
        async with Client(parameters, mode=mode, cache=None) as client:
            yield client
    else:
        assert settings is not None
        async with authorized_http(settings) as http:
            async with Client(
                streamable_http_client(endpoint(settings), http_client=http), mode=mode, cache=None
            ) as client:
                yield client
