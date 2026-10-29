"""Bounded interactive QR authorization through Telethon's public login API."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import errors

from telegram_mcp.domain.models import ToolError

MAX_QR_CODES = 3
MAX_QR_WAIT_SECONDS = 60


async def _wait_token(qr: Any) -> Any:
    # qr.wait() observes Telegram's expiry; also cap unexpectedly long lifetimes.
    async with asyncio.timeout(MAX_QR_WAIT_SECONDS):
        return await qr.wait()


async def _scan(qr: Any, show_qr: Callable[[str], None]) -> Any:
    waiting = asyncio.create_task(_wait_token(qr))
    try:
        # Register Telethon's update handler before the user can scan the code.
        await asyncio.sleep(0)
        if not waiting.done():
            show_qr(qr.url)
        try:
            return await waiting
        except (TimeoutError, errors.AuthTokenExpiredError):
            return None
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def _authorize(
    client: Any,
    show_qr: Callable[[str], None],
    password: Callable[[], str],
    timeout_seconds: float,
) -> Any:
    async def rpc(operation: Awaitable[Any]) -> Any:
        return await asyncio.wait_for(operation, timeout_seconds)

    await rpc(client.connect())
    user = await rpc(client.get_me())
    if user is not None:
        return user
    for _ in range(MAX_QR_CODES):
        qr = await rpc(client.qr_login())
        try:
            user = await _scan(qr, show_qr)
        except errors.SessionPasswordNeededError:
            # Only the user types this into the local, non-echoing password prompt.
            return await rpc(client.sign_in(password=password()))
        if user is not None:
            return user
    raise ToolError(
        "QR_LOGIN_EXPIRED",
        "No QR code was accepted before expiry. Run login --qr again when the phone is ready.",
    )


async def authorize_qr(
    client: Any,
    *,
    show_qr: Callable[[str], None],
    password: Callable[[], str],
    timeout_seconds: float,
) -> Any:
    try:
        return await _authorize(client, show_qr, password, timeout_seconds)
    except (ToolError, EOFError):
        raise
    except (
        errors.FloodWaitError,
        errors.FloodPremiumWaitError,
        errors.FloodTestPhoneWaitError,
    ) as exc:
        raise ToolError(
            "RATE_LIMITED", f"Telegram requests a wait of {exc.seconds} seconds.", exc.seconds
        ) from None
    except errors.PasswordHashInvalidError:
        raise ToolError(
            "AUTH_PASSWORD_INVALID", "The 2FA password was rejected. Run login --qr again."
        ) from None
    except (TimeoutError, ConnectionError, OSError):
        raise ToolError(
            "CONNECTION_ERROR", "Telegram request timed out or the connection failed. Retry later."
        ) from None
    except (errors.ServerError, errors.TimedOutError):
        raise ToolError(
            "TELEGRAM_UNAVAILABLE", "Telegram is temporarily unavailable. Retry later."
        ) from None
    except Exception:
        # Telethon protocol errors may include a login token or authorization payload.
        raise ToolError(
            "QR_LOGIN_FAILED",
            "QR login could not be completed. Retry later or use login with a code.",
        ) from None
