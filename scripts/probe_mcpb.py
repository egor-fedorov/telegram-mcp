"""Run inside the bundle's environment; all server state is synthetic and disposable."""

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import uvicorn
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types import PromptReference

import telegram_mcp
from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import Settings, save_settings
from telegram_mcp.adapters.downloads import DownloadStore
from telegram_mcp.adapters.exports import ExportStore
from telegram_mcp.adapters.http_token import load_token
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.adapters.telegram import TelegramBackend
from telegram_mcp.adapters.transport import create_http_app
from telegram_mcp.adapters.watches import WatchStore
from telegram_mcp.bootstrap import build_service


def launch_config(extension: Path, config: Path) -> tuple[str, list[str]]:
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    launch = manifest["server"]["mcp_config"]
    arguments = [
        argument.replace("${__dirname}", str(extension)).replace(
            "${user_config.config_file}", str(config)
        )
        for argument in launch["args"]
    ]
    return launch["command"], arguments


async def check_client(client: Client, expected_version: str) -> None:
    assert client.server_info and client.server_info.version == expected_version
    assert len((await client.list_tools()).tools) == 23
    response = await client.call_tool("get_status", {})
    assert not response.is_error and response.structured_content
    assert response.structured_content["authorized"] is False
    assert response.structured_content["error"]["code"] == "AUTH_REQUIRED"
    assert {p.name for p in (await client.list_prompts()).prompts} == {
        "period_digest",
        "watch_digest",
    }
    assert (await client.get_prompt("period_digest", {"chat_ref": "1", "days": "1"})).messages
    result = await client.complete(
        PromptReference(name="watch_digest"), {"name": "watch_name", "value": "missing"}
    )
    assert result.completion.values == []
    assert (await client.list_resource_templates()).resource_templates
    try:
        await client.read_resource("telegram-attachment://downloads/missing")
    except MCPError:
        pass
    else:
        raise AssertionError("Missing resource must remain an MCP error.")


async def probe(extension: Path, root: Path, expected_version: str) -> Path:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        settings = Settings(
            session_path=root / "unused.session",
            http_token_path=root / "mcp.token",
            http_port=listener.getsockname()[1],
        )
        config = root / "shared configuration.toml"
        save_settings(settings, config)
        clock = SystemClock()
        service = build_service(
            TelegramBackend(settings),
            settings,
            downloads=DownloadStore(settings.session_path, clock, root=root / "downloads"),
            checkpoints=CheckpointStore(settings.session_path, clock, root=root / "checkpoints"),
            exports=ExportStore(settings.session_path, clock, root=root / "exports"),
            watches=WatchStore(settings.session_path, root=root / "watches"),
        )
        token = load_token(settings.http_token_path, create=True)
        app = create_http_app(service, token, settings.http_port)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            command, arguments = launch_config(extension, config)
            parameters = StdioServerParameters(command=command, args=arguments)
            # An existing owner must not affect a proxy or lose its lease.
            lease = SessionLease(settings.session_path)
            lease.acquire()
            try:
                async with Client(parameters, cache=None) as first:
                    async with Client(parameters, mode="legacy", cache=None) as second:
                        await check_client(first, expected_version)
                        await check_client(second, expected_version)
                    await check_client(first, expected_version)
                assert not task.done()
                assert lease.held
            finally:
                lease.close()
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 15)
    return config


def main() -> None:
    extension, root = Path(sys.argv[1]), Path.cwd()
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    assert version("telegram-context-mcp") == manifest["version"]
    assert telegram_mcp.__file__
    assert Path(telegram_mcp.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    session = root / "unused.session"
    session.write_bytes(b"Synthetic session: never connect to Telegram.\n")
    config = asyncio.run(probe(extension, root, manifest["version"]))
    command, arguments = launch_config(extension, config)
    result = subprocess.run(
        [command, *arguments], capture_output=True, text=True, timeout=45, check=False
    )
    assert result.returncode == 1 and "SHARED_SERVER_UNAVAILABLE" in result.stderr
    assert not result.stdout
    assert (root / "mcp.token").read_text(encoding="utf-8").strip() not in result.stderr
    profile = [config, root / "mcp.token", session]
    snapshot = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in profile}
    (root / "profile-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    assert session.read_bytes() == b"Synthetic session: never connect to Telegram.\n"
    print(
        "Extracted MCPB: tools, prompts, completion, resource errors and shared ownership passed."
    )


if __name__ == "__main__":
    main()
