"""Authenticated loopback HTTP serving one application and Telegram runtime."""

import asyncio
import secrets
import socket
from typing import Literal

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import ToolError

HOST = "127.0.0.1"


def endpoint(settings: Settings) -> str:
    return f"http://{HOST}:{settings.http_port}/mcp"


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self._token = token.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
            if (
                len(headers) != 1
                or headers[0][:7].lower() != b"bearer "
                or not secrets.compare_digest(headers[0][7:], self._token)
            ):
                response = JSONResponse(
                    {"error": "Authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(
    service: Service,
    token: str,
    port: int,
    *,
    owns_service: bool = True,
    schema_mode: Literal["compact", "full"] = "compact",
) -> Starlette:
    server = create_server(service, owns_service=owns_service, schema_mode=schema_mode)
    hosts = [f"{HOST}:{port}"]
    origins = [f"http://{HOST}:{port}"]
    if port == 80:
        hosts.append(HOST)
        origins.append(f"http://{HOST}")
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=HOST,
        transport_security=TransportSecuritySettings(
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


async def _serve_http(service: Service, settings: Settings) -> None:
    try:
        await _run_http(service, settings)
    finally:
        await service.close()


async def _run_http(service: Service, settings: Settings) -> None:
    token = load_token(settings.http_token_path, create=True)
    app = create_http_app(
        service, token, settings.http_port, owns_service=False, schema_mode=settings.mcp_schema_mode
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((HOST, settings.http_port))
        except OSError:
            raise ToolError(
                "HTTP_BIND_FAILED",
                "Cannot bind the loopback HTTP port. Stop the other server or choose "
                "a different http_port in the configuration.",
            ) from None
        listener.setblocking(False)
        config = uvicorn.Config(
            app,
            host=HOST,
            port=settings.http_port,
            limit_concurrency=16,
            access_log=False,
            log_level="warning",
        )
        await uvicorn.Server(config).serve(sockets=[listener])


def run_http(service: Service, settings: Settings) -> None:
    asyncio.run(_serve_http(service, settings))
