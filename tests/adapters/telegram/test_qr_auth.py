"""QR approval, expiry and failure handling through public Telethon methods."""

import asyncio
from unittest.mock import Mock

import pytest
from telethon import errors

from telegram_mcp.adapters.telegram import qr_auth
from telegram_mcp.domain.models import ToolError
from tests.support.qr_auth import PASSWORD, TOKEN, USER, FakeQR, qr_client, scan_codes


async def test_scan_handler_is_ready_before_the_qr_is_displayed() -> None:
    qr = FakeQR()
    client, password = qr_client([qr]), Mock()
    assert (
        await qr_auth.authorize_qr(
            client, show_qr=scan_codes([qr]), password=password, timeout_seconds=1
        )
        is USER
    )
    assert qr.handlers == 0
    password.assert_not_called()
    client.start.assert_not_called()
    client.sign_in.assert_not_called()


async def test_expired_tokens_refresh_and_the_latest_code_can_succeed() -> None:
    qrs = [FakeQR("timeout"), FakeQR("expired"), FakeQR()]
    client = qr_client(qrs)
    assert (
        await qr_auth.authorize_qr(
            client, show_qr=scan_codes(qrs), password=Mock(), timeout_seconds=1
        )
        is USER
    )
    assert client.qr_login.await_count == 3
    assert all(qr.handlers == 0 for qr in qrs)


async def test_expiry_stops_after_three_codes() -> None:
    qrs = [FakeQR("timeout") for _ in range(3)]
    client = qr_client(qrs)
    with pytest.raises(ToolError, match="QR_LOGIN_EXPIRED"):
        await qr_auth.authorize_qr(
            client, show_qr=scan_codes(qrs), password=Mock(), timeout_seconds=1
        )
    assert client.qr_login.await_count == 3 and all(qr.handlers == 0 for qr in qrs)


async def test_stalled_wait_is_bounded_and_removes_its_handler(monkeypatch) -> None:
    monkeypatch.setattr(qr_auth, "MAX_QR_WAIT_SECONDS", 0.02)
    qrs = [FakeQR() for _ in range(3)]
    client = qr_client(qrs)
    with pytest.raises(ToolError, match="QR_LOGIN_EXPIRED"):
        await qr_auth.authorize_qr(client, show_qr=Mock(), password=Mock(), timeout_seconds=1)
    assert all(qr.started.is_set() and qr.handlers == 0 for qr in qrs)


async def test_existing_authorization_does_not_request_a_qr_or_password() -> None:
    client, display, password = qr_client([], authorized=True), Mock(), Mock()
    assert (
        await qr_auth.authorize_qr(client, show_qr=display, password=password, timeout_seconds=1)
        is USER
    )
    display.assert_not_called()
    password.assert_not_called()
    client.qr_login.assert_not_called()
    client.sign_in.assert_not_called()


@pytest.mark.parametrize("accepted", [True, False])
async def test_two_factor_password_is_requested_only_after_qr_approval(accepted: bool) -> None:
    qr = FakeQR("password")
    client, password = qr_client([qr]), Mock(return_value=PASSWORD)
    if not accepted:
        client.sign_in.side_effect = errors.PasswordHashInvalidError(None)
    operation = qr_auth.authorize_qr(
        client, show_qr=scan_codes([qr]), password=password, timeout_seconds=1
    )
    if accepted:
        assert await operation is USER
    else:
        with pytest.raises(ToolError, match="AUTH_PASSWORD_INVALID") as caught:
            await operation
        assert PASSWORD not in str(caught.value) and TOKEN not in str(caught.value)
    password.assert_called_once_with()
    client.sign_in.assert_awaited_once_with(password=PASSWORD)
    assert client.qr_login.await_count == 1 and qr.handlers == 0


@pytest.mark.parametrize("stage", ["connect", "qr_login", "scan", "password"])
async def test_flood_wait_is_reported_without_sleeping_or_retrying(stage: str) -> None:
    error = errors.FloodWaitError(None, capture=1234)
    qr = FakeQR("password" if stage == "password" else "success")
    client = qr_client([qr])
    if stage in {"connect", "qr_login"}:
        getattr(client, stage).side_effect = error
    elif stage == "scan":
        qr.error = error
    else:
        client.sign_in.side_effect = error
    with pytest.raises(ToolError, match="RATE_LIMITED") as caught:
        await asyncio.wait_for(
            qr_auth.authorize_qr(
                client, show_qr=scan_codes([qr]), password=lambda: PASSWORD, timeout_seconds=1
            ),
            2,
        )
    assert caught.value.retry_after == 1234 and client.qr_login.await_count <= 1
    assert qr.handlers == 0


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ConnectionError(TOKEN), "CONNECTION_ERROR"),
        (errors.ServerError(None, TOKEN, 500), "TELEGRAM_UNAVAILABLE"),
        (errors.RPCError(None, TOKEN, 400), "QR_LOGIN_FAILED"),
        (TypeError(TOKEN), "QR_LOGIN_FAILED"),
    ],
)
async def test_failures_do_not_expose_tokens_or_raw_responses(error, code) -> None:
    client = qr_client([])
    client.qr_login.side_effect = error
    with pytest.raises(ToolError, match=code) as caught:
        await qr_auth.authorize_qr(client, show_qr=Mock(), password=Mock(), timeout_seconds=1)
    assert TOKEN not in str(caught.value)


async def test_network_deadline_does_not_request_a_qr() -> None:
    client = qr_client([])
    client.connect.side_effect = asyncio.Event().wait
    with pytest.raises(ToolError, match="CONNECTION_ERROR"):
        await qr_auth.authorize_qr(client, show_qr=Mock(), password=Mock(), timeout_seconds=0.02)
    client.qr_login.assert_not_called()


async def test_renderer_failure_cancels_the_scan_waiter() -> None:
    qr = FakeQR()
    with pytest.raises(ToolError, match="QR_LOGIN_FAILED") as caught:
        await qr_auth.authorize_qr(
            qr_client([qr]),
            show_qr=Mock(side_effect=RuntimeError(TOKEN)),
            password=Mock(),
            timeout_seconds=1,
        )
    assert TOKEN not in str(caught.value) and qr.handlers == 0


async def test_cancellation_removes_the_scan_handler() -> None:
    qr = FakeQR()
    task = asyncio.create_task(
        qr_auth.authorize_qr(qr_client([qr]), show_qr=Mock(), password=Mock(), timeout_seconds=1)
    )
    await qr.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert qr.handlers == 0
