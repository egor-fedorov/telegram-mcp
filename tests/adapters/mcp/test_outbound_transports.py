"""Real STDIO, authenticated HTTP and connect confirmation use only fake Telegram writes."""

import asyncio
import sys

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ElicitResult

from telegram_mcp.adapters.config import save_settings
from telegram_mcp.adapters.transport import endpoint
from tests.support.outbound import FakeOutbound
from tests.support.transport import authorized_http, running_server


async def exercise(client, expected):
    result = await client.call_tool(
        "send_message",
        {"chat_ref": "1", "text": "Only synthetic content", "idempotency_key": "transport-test"},
    )
    assert not result.is_error, result
    assert result.structured_content["status"] == expected, result
    status = await client.call_tool(
        "get_send_status", {"operation_id": result.structured_content["operation_id"]}
    )
    assert not status.is_error, status
    assert status.structured_content == {"result": result.structured_content}
    return result


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_stdio_confirmation(tmp_path, mode):
    forms = []

    async def confirm(context, params):
        forms.append(params.message)
        return ElicitResult(action="accept", content={"approve": True})

    script = (
        "from pathlib import Path; import sys; "
        "from tests.support.outbound import outbound_service; "
        "from telegram_mcp.adapters.mcp import create_server; "
        "create_server(outbound_service(Path(sys.argv[1])).service).run()"
    )
    parameters = StdioServerParameters(command=sys.executable, args=["-c", script, str(tmp_path)])
    async with Client(parameters, mode=mode, elicitation_callback=confirm) as client:
        await exercise(client, "sent")
        assert len(forms) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("transport", ["http", "connect"])
@pytest.mark.parametrize("supported", [False, True])
async def test_http_and_connect_confirmation(tmp_path, mode, transport, supported):
    telegram = FakeOutbound()
    forms = []

    async def confirm(context, params):
        forms.append(params.message)
        return ElicitResult(action="accept", content={"approve": True})

    callback = confirm if supported else None
    async with running_server(tmp_path, outbound_backend=telegram) as (settings, _):
        if transport == "http":
            async with authorized_http(settings) as http:
                async with Client(
                    streamable_http_client(endpoint(settings), http_client=http),
                    mode=mode,
                    elicitation_callback=callback,
                    read_timeout_seconds=20,
                ) as client:
                    await exercise(client, "sent" if supported else "failed")
        else:
            config = tmp_path / "config.toml"
            save_settings(settings, config)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "telegram_mcp.cli", "--config", str(config), "connect"],
            )
            async with Client(
                params, mode=mode, elicitation_callback=callback, read_timeout_seconds=20
            ) as client:
                await exercise(client, "sent" if supported else "failed")
        assert len(forms) == int(supported) and len(telegram.send_calls) == int(supported)


async def test_two_http_clients_cannot_receive_each_others_confirmation(tmp_path):
    telegram = FakeOutbound()
    received = {"first": [], "second": []}

    async def session(settings, label):
        async def confirm(context, params):
            received[label].append(params.message)
            return ElicitResult(action="accept", content={"approve": label == "first"})

        async with authorized_http(settings) as http:
            async with Client(
                streamable_http_client(endpoint(settings), http_client=http),
                mode="legacy",
                elicitation_callback=confirm,
            ) as client:
                return await client.call_tool(
                    "send_message", {"chat_ref": "1", "text": label, "idempotency_key": label}
                )

    async with running_server(tmp_path, outbound_backend=telegram) as (settings, _):
        first, second = await asyncio.gather(
            session(settings, "first"), session(settings, "second")
        )
        assert first.structured_content["status"] == "sent"
        assert second.structured_content["status"] == "failed"
    assert len(telegram.send_calls) == 1 and telegram.send_calls[0][1].text == "first"
    assert len(received["first"]) == len(received["second"]) == 1
    assert "Text/caption:\nfirst" in received["first"][0]
    assert "Text/caption:\nsecond" in received["second"][0]
