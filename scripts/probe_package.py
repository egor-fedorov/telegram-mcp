"""Run inside a clean installation, outside the source checkout, without credentials."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

import telegram_mcp
from telegram_mcp.adapters.version import get_version


async def probe_client(client: Client, version: str, contract: dict[str, object]) -> None:
    assert client.server_info is not None
    assert client.server_info.version == version
    definitions = {}
    tools = {}
    for tool in (await client.list_tools()).tools:
        entry = {
            "input": dict(tool.input_schema),
            "output": dict(tool.output_schema or {}),
            "annotations": tool.annotations.model_dump(mode="json") if tool.annotations else None,
        }
        for schema in (entry["input"], entry["output"]):
            for name, value in schema.pop("$defs", {}).items():
                if name in definitions:
                    assert definitions[name] == value
                definitions[name] = value
        tools[tool.name] = entry
    assert {"definitions": definitions, "tools": tools} == contract
    response = await client.call_tool("get_status", {})
    assert not response.is_error
    status = response.structured_content
    assert status is not None
    assert status["authorized"] is False
    assert status["error"]["code"] == "AUTH_REQUIRED"


async def probe_stdio(
    executable: Path,
    config: Path,
    version: str,
    contract: dict[str, object],
    command: str = "serve",
) -> None:
    parameters = StdioServerParameters(
        command=str(executable),
        args=["--config", str(config), command],
        env={
            "TELEGRAM_API_ID": "",
            "TELEGRAM_API_HASH": "",
            "TELEGRAM_SESSION_PATH": str(config.parent / "unused.session"),
            "PYTHONUTF8": "1",
        },
    )
    for mode in ("auto", "legacy"):
        async with Client(parameters, mode=mode, read_timeout_seconds=15) as client:
            await probe_client(client, version, contract)


async def probe_http(
    executable: Path,
    config: Path,
    token_path: Path,
    port: int,
    version: str,
    contract: dict[str, object],
) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    async with httpx2.AsyncClient(timeout=1, trust_env=False) as anonymous:
        for attempt in range(100):
            try:
                response = await anonymous.get(url)
                assert response.status_code == 401
                break
            except httpx2.TransportError:
                if attempt == 99:
                    raise
                await asyncio.sleep(0.1)
    token = (await asyncio.to_thread(token_path.read_text, encoding="utf-8")).strip()
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=15, trust_env=False
    ) as http:
        for mode in ("auto", "legacy"):
            async with Client(
                streamable_http_client(url, http_client=http), mode=mode, cache=None
            ) as client:
                await probe_client(client, version, contract)
    await probe_stdio(executable, config, version, contract, command="connect")


def probe_shared_server(executable: Path, version: str, contract: dict[str, object]) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = Path.cwd() / "shared.toml"
    token = Path.cwd() / "mcp.token"
    config.write_text(
        f"http_port = {port}\nhttp_token_path = {json.dumps(str(token))}\n"
        f"session_path = {json.dumps(str(Path.cwd() / 'unused.session'))}\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [str(executable), "--config", str(config), "serve", "--transport", "http"],
        stdout=subprocess.DEVNULL,
    )
    try:
        asyncio.run(probe_http(executable, config, token, port, version, contract))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    assert telegram_mcp.__file__ is not None
    assert Path(telegram_mcp.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    metadata = distribution("telegram-context-mcp")
    assert metadata.version == get_version() == args.version
    assert metadata.metadata["License-Expression"] == "MIT"
    executable = Path(sys.executable).parent / (
        "telegram-mcp.exe" if os.name == "nt" else "telegram-mcp"
    )
    version = subprocess.check_output([str(executable), "--version"], text=True, timeout=15)
    assert version.strip() == f"telegram-mcp {args.version}"
    subprocess.run([str(executable), "--help"], check=True, capture_output=True, timeout=15)
    contract = json.loads(Path(__file__).with_name("mcp_contract.json").read_text(encoding="utf-8"))
    asyncio.run(probe_stdio(executable, Path.cwd() / "missing.toml", args.version, contract))
    probe_shared_server(executable, args.version, contract)
    assert not list(Path.cwd().glob("*.session*"))
    print(f"Installed package {args.version}: metadata, CLI, STDIO, HTTP, and bridge passed.")


if __name__ == "__main__":
    main()
