import asyncio
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.transport import create_http_app
from telegram_mcp.bootstrap import build_service
from tests.support.fakes import FakeBackend, msg
from tests.support.resources import (
    ResourceService,
)


@asynccontextmanager
async def running_server(tmp_path: Path) -> AsyncGenerator[tuple[Settings, FakeBackend]]:
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
