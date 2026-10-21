"""One Telegram client, connection lifecycle, and translation of transport failures."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, TypeVar

from telethon import TelegramClient, errors, functions

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.version import get_version
from telegram_mcp.domain.models import ToolError

T = TypeVar("T")


class TelegramRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[..., Any] = TelegramClient,
        client: Any = None,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.client = client
        self.lease = SessionLease(settings.session_path)
        self.connect_lock = asyncio.Lock()

    async def call(self, request: Awaitable[T]) -> T:
        try:
            return await asyncio.wait_for(request, self.settings.rpc_timeout_seconds)
        except errors.FloodWaitError as exc:
            raise ToolError(
                "RATE_LIMITED", f"Telegram requests a wait of {exc.seconds} seconds.", exc.seconds
            ) from None
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError):
            raise ToolError(
                "AUTH_REQUIRED", "Telegram session is no longer authorized. Run login."
            ) from None
        except (errors.ServerError, errors.TimedOutError):
            raise ToolError(
                "TELEGRAM_UNAVAILABLE", "Telegram is temporarily unavailable. Retry this request."
            ) from None
        except (errors.QueryTooShortError, errors.SearchQueryEmptyError):
            raise ToolError(
                "INVALID_ARGUMENT", "Telegram rejected an empty or too-short search query."
            ) from None
        except errors.TopicDeletedError:
            raise ToolError(
                "TOPIC_UNAVAILABLE", "The requested forum topic is unavailable."
            ) from None
        except errors.ChannelForumMissingError:
            raise ToolError(
                "FORUM_UNAVAILABLE", "This chat is not a supported supergroup forum."
            ) from None
        except errors.RPCError as exc:
            if exc.message == "TOPIC_ID_INVALID":
                raise ToolError(
                    "TOPIC_UNAVAILABLE", "The requested forum topic is unavailable."
                ) from None
            raise ToolError("TELEGRAM_ERROR", type(exc).__name__) from None
        except (TimeoutError, ConnectionError, OSError):
            raise ToolError(
                "CONNECTION_ERROR", "Telegram request timed out or connection failed."
            ) from None
        except (ValueError, TypeError):
            raise ToolError(
                "INVALID_REFERENCE", "Telegram could not resolve the requested entity."
            ) from None

    async def ensure(self) -> Any:
        async with self.connect_lock:
            if self.client is not None:
                return self.client
            api_id, api_hash = self.settings.credentials()
            path = self.settings.session_path
            if not path.is_file():
                raise ToolError(
                    "AUTH_REQUIRED", "No saved Telegram session. Run telegram-mcp login."
                )
            self.lease.acquire()
            client: Any = None
            try:
                try:
                    client = self.client_factory(
                        str(path),
                        api_id,
                        api_hash,
                        flood_sleep_threshold=0,
                        request_retries=1,
                        connection_retries=1,
                        retry_delay=1,
                        raise_last_call_error=True,
                        device_model="Telegram Context MCP",
                        app_version=get_version(),
                    )
                except (sqlite3.Error, OSError):
                    raise ToolError(
                        "SESSION_ERROR", "Cannot open the local Telegram session database."
                    ) from None
                await self.call(client.connect())
                await self.call(client(functions.updates.GetStateRequest()))
                self.client = client
            except BaseException:
                try:
                    if client is not None:
                        with suppress(Exception):
                            await self.call(client.disconnect())
                finally:
                    self.lease.close()
                raise
            return client

    async def close(self) -> None:
        try:
            if self.client is not None:
                await self.call(self.client.disconnect())
        finally:
            self.client = None
            self.lease.close()

    async def status(self) -> dict[str, object]:
        try:
            existing_client = self.client
            client = await self.ensure()
            if existing_client is not None:
                await self.call(client(functions.updates.GetStateRequest()))
            return {"authorized": True, "connected": bool(client.is_connected())}
        except ToolError as exc:
            return {
                "authorized": False if exc.code == "AUTH_REQUIRED" else None,
                "connected": bool(self.client is not None and self.client.is_connected()),
                "error": exc.issue().model_dump(mode="json"),
            }
