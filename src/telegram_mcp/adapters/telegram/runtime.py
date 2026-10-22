"""One Telegram client, connection lifecycle, and translation of transport failures."""

import asyncio
import math
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, TypeVar

from telethon import TelegramClient, errors, functions

from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.version import get_version
from telegram_mcp.application.ports import Clock
from telegram_mcp.domain.models import ToolError

T = TypeVar("T")


class TelegramRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[..., Any] = TelegramClient,
        client: Any = None,
        clock: Clock | None = None,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.client = client
        self.lease = SessionLease(settings.session_path)
        self.connect_lock = asyncio.Lock()
        self.clock = clock if clock is not None else SystemClock()
        self.generation = 0
        self.auth_generation = 0
        self._authorization_checks = 0
        self._needs_probe = False
        self._retry_at = 0.0
        self._retry_error: ToolError | None = None
        self._closed = False

    async def call(self, request: Awaitable[T]) -> T:
        try:
            return await asyncio.wait_for(request, self.settings.rpc_timeout_seconds)
        except (
            errors.FloodWaitError,
            errors.FloodPremiumWaitError,
            errors.FloodTestPhoneWaitError,
        ) as exc:
            raise ToolError(
                "RATE_LIMITED", f"Telegram requests a wait of {exc.seconds} seconds.", exc.seconds
            ) from None
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError):
            self.generation += 1
            self.auth_generation += 1
            self._needs_probe = True
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
            self.generation += 1
            raise ToolError(
                "CONNECTION_ERROR", "Telegram request timed out or connection failed."
            ) from None
        except (ValueError, TypeError):
            raise ToolError(
                "INVALID_REFERENCE", "Telegram could not resolve the requested entity."
            ) from None

    async def ensure(self) -> Any:
        async with self.connect_lock:
            return await self._ensure_locked()

    async def _ensure_locked(self) -> Any:
        if self._closed:
            raise ToolError("CONNECTION_ERROR", "The Telegram runtime has been closed.")
        if self.client is not None:
            if self.client.is_connected() and not self._needs_probe:
                return self.client
            self._check_cooldown()
            try:
                if not self.client.is_connected():
                    self._needs_probe = True
                    await self.call(self.client.connect())
                    self.generation += 1
                await self._probe(self.client)
            except ToolError as exc:
                self._set_cooldown(exc)
                raise
            return self.client
        self._check_cooldown()
        api_id, api_hash = self.settings.credentials()
        path = self.settings.session_path
        if not path.is_file():
            raise ToolError("AUTH_REQUIRED", "No saved Telegram session. Run telegram-mcp login.")
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
            await self._probe(client)
            self.client = client
            self.generation += 1
        except BaseException as exc:
            if isinstance(exc, ToolError):
                self._set_cooldown(exc)
            try:
                if client is not None:
                    with suppress(Exception):
                        await self.call(client.disconnect())
            finally:
                self.lease.close()
            raise
        return client

    def _check_cooldown(self) -> None:
        if self._retry_error is not None and self.clock.monotonic() < self._retry_at:
            raise ToolError(
                self._retry_error.code,
                self._retry_error.detail,
                max(1, math.ceil(self._retry_at - self.clock.monotonic())),
            )

    def _set_cooldown(self, error: ToolError) -> None:
        if error.code in ("CONNECTION_ERROR", "TELEGRAM_UNAVAILABLE", "RATE_LIMITED"):
            seconds = error.retry_after if error.code == "RATE_LIMITED" else 5
            error.retry_after = seconds
            self._retry_at = self.clock.monotonic() + (seconds or 0)
            self._retry_error = error

    async def _probe(self, client: Any) -> None:
        self._needs_probe = True
        auth_generation = self.auth_generation
        await self.call(client(functions.updates.GetStateRequest()))
        if auth_generation != self.auth_generation:
            raise ToolError("AUTH_REQUIRED", "Telegram session is no longer authorized. Run login.")
        self._authorization_checks += 1
        self._needs_probe = False
        self._retry_error = None

    async def verify_authorization(self) -> None:
        async with self.connect_lock:
            checks = self._authorization_checks
            client = await self._ensure_locked()
            if checks == self._authorization_checks:
                try:
                    await self._probe(client)
                except ToolError as exc:
                    self._set_cooldown(exc)
                    raise

    async def close(self) -> None:
        async with self.connect_lock:
            self._closed = True
            try:
                if self.client is not None:
                    await self.call(self.client.disconnect())
            finally:
                self.client = None
                self.generation += 1
                self.lease.close()

    async def status(self) -> dict[str, object]:
        try:
            await self.verify_authorization()
            client = self.client
            return {
                "authorized": True,
                "connected": bool(client is not None and client.is_connected()),
            }
        except ToolError as exc:
            return {
                "authorized": False if exc.code == "AUTH_REQUIRED" else None,
                "connected": bool(self.client is not None and self.client.is_connected()),
                "error": exc.issue().model_dump(mode="json"),
            }
