"""Local authentication and configuration boundaries without a Telegram account."""

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from telegram_mcp.adapters.cli import login
from telegram_mcp.adapters.config import ConfiguredPolicyProvider, Settings
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.telegram.auth import authenticate
from telegram_mcp.domain.models import ToolError
from tests.support.qr_auth import TOKEN, FakeQR, qr_client, scan_codes


def settings_for(path: Path) -> Settings:
    return Settings(api_id=123, api_hash=SecretStr("synthetic"), session_path=path)


@pytest.mark.parametrize("failure", [None, "start", "bot", "cancel", "constructor"])
async def test_authentication_releases_session_and_restores_umask(
    tmp_path: Path, failure: str | None
) -> None:
    session = tmp_path / "login.session"
    session.touch()
    settings = settings_for(session)
    client = Mock(start=AsyncMock(), get_me=AsyncMock(), disconnect=AsyncMock())
    client.get_me.return_value = SimpleNamespace(bot=failure == "bot")
    original_mask = os.umask(0o077)
    os.umask(original_mask)
    probe = SessionLease(session)

    async def start(**kwargs: object) -> None:
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            probe.acquire()
        assert set(kwargs) == {"phone", "code_callback", "password"}
        if failure == "start":
            raise ConnectionError("synthetic failure")
        if failure == "cancel":
            raise asyncio.CancelledError()

    client.start.side_effect = start
    constructor = Mock(return_value=client)
    if failure == "constructor":
        constructor.side_effect = OSError("synthetic failure")
    operation = authenticate(
        settings,
        phone=lambda: "synthetic phone",
        code=lambda: "synthetic code",
        password=lambda: "synthetic password",
        client_factory=constructor,
    )
    if failure is None:
        await operation
        if os.name != "nt":
            assert stat.S_IMODE(session.stat().st_mode) == 0o600
    else:
        expected = {
            "start": ConnectionError,
            "bot": ToolError,
            "cancel": asyncio.CancelledError,
            "constructor": OSError,
        }[failure]
        with pytest.raises(expected):
            await operation
    if failure != "constructor":
        client.disconnect.assert_awaited_once()
    observed_mask = os.umask(original_mask)
    assert observed_mask == original_mask
    probe.acquire()
    probe.close()


async def test_login_requires_tty_before_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("telegram_mcp.adapters.cli.sys.stdin.isatty", lambda: False)
    authenticate = AsyncMock()
    with pytest.raises(ToolError, match="INTERACTIVE_LOGIN_REQUIRED"):
        await login(Settings(), tmp_path / "config.toml", authenticate)
    authenticate.assert_not_called()
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("failure", [None, "constructor", "disconnect", "scan", "bot", "cancel"])
async def test_qr_authentication_preserves_the_session_lease_and_cleanup(tmp_path, failure):
    session = tmp_path / "qr.session"
    session.touch()
    qr = FakeQR(error=RuntimeError(TOKEN) if failure == "scan" else None)
    client = qr_client([qr])
    if failure == "bot":
        client.get_me.return_value = SimpleNamespace(bot=True)
    if failure == "disconnect":
        client.disconnect.side_effect = RuntimeError(TOKEN)
    constructor = Mock(return_value=client)
    if failure == "constructor":
        constructor.side_effect = RuntimeError(TOKEN)
    probe = SessionLease(session)

    async def connect():
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            probe.acquire()

    client.connect.side_effect = connect
    unused = Mock(side_effect=AssertionError("QR login must not request phone/code/password here"))
    original_mask = os.umask(0o077)
    os.umask(original_mask)
    operation = authenticate(
        settings_for(session),
        phone=unused,
        code=unused,
        password=unused,
        show_qr=Mock() if failure == "cancel" else scan_codes([qr]),
        client_factory=constructor,
    )
    if failure == "cancel":
        task = asyncio.create_task(operation)
        await qr.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif failure is not None:
        code = "USER_ACCOUNT_REQUIRED" if failure == "bot" else "QR_LOGIN_FAILED"
        with pytest.raises(ToolError, match=code) as caught:
            await operation
        assert TOKEN not in str(caught.value)
    else:
        await operation
        if os.name != "nt":
            assert stat.S_IMODE(session.stat().st_mode) == 0o600
    unused.assert_not_called()
    client.start.assert_not_called()
    if failure != "constructor":
        client.disconnect.assert_awaited_once()
    assert constructor.call_args.kwargs["flood_sleep_threshold"] == 0
    assert constructor.call_args.kwargs["raise_last_call_error"] is True
    assert qr.handlers == 0
    assert os.umask(original_mask) == original_mask
    probe.acquire()
    probe.close()


async def test_qr_login_never_opens_a_session_owned_by_another_process(tmp_path):
    session = tmp_path / "owned.session"
    lease = SessionLease(session)
    lease.acquire()
    factory = Mock()
    try:
        with pytest.raises(ToolError, match="SESSION_IN_USE"):
            await authenticate(
                settings_for(session),
                phone=Mock(),
                code=Mock(),
                password=Mock(),
                show_qr=Mock(),
                client_factory=factory,
            )
        factory.assert_not_called()
    finally:
        lease.close()


def test_policy_provider_snapshots_current_rules_without_credentials() -> None:
    settings = settings_for(Path("synthetic.session"))
    settings.allowed_chat_ids = ["1"]
    provider = ConfiguredPolicyProvider(settings)
    first = provider.current()
    settings.allowed_chat_ids.clear()
    settings.allow_public_channel_reads = True
    second = provider.current()
    assert first.allowed_chat_ids == ("1",)
    assert second.allowed_chat_ids == () and second.allow_public_channel_reads
    assert "synthetic" not in repr(first)
    assert not hasattr(first, "api_hash") and not hasattr(first, "session_path")
