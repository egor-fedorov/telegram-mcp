"""Local commands for login, selecting scope, diagnosis and serving MCP."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr, ValidationError

from telegram_mcp.adapters.config import Settings, config_path, load_settings, save_settings
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.application.ports import Backend
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import ToolError


async def login(
    settings: Settings, path: Path, authenticate: Callable[..., Awaitable[None]]
) -> None:
    if not sys.stdin.isatty():
        raise ToolError(
            "INTERACTIVE_LOGIN_REQUIRED", "Run login yourself in an interactive terminal."
        )
    if settings.api_id is None:
        settings.api_id = int(
            (await asyncio.to_thread(input, "Telegram api_id (my.telegram.org): ")).strip()
        )
    if settings.api_hash is None:
        settings.api_hash = SecretStr(getpass.getpass("Telegram api_hash: ").strip())
    await authenticate(
        settings,
        phone=lambda: input("Phone number: "),
        code=lambda: getpass.getpass("Telegram login code: "),
        password=lambda: getpass.getpass("Telegram 2FA password: "),
    )
    save_settings(settings, path)
    print(f"Session saved. Configuration: {path}")
    print("Next: telegram-mcp configure — choose the folders/chats the server may read.")


async def prompt_boolean(prompt: str, current: bool) -> bool:
    current_label = "yes" if current else "no"
    while True:
        value = await asyncio.to_thread(input, f"{prompt} [y/n] (Enter: keep {current_label}): ")
        value = value.strip().lower()
        if not value:
            return current
        if value in {"y", "yes", "n", "no"}:
            return value in {"y", "yes"}
        print("Enter y or n, or press Enter to keep the current setting.")


async def configure(
    settings: Settings, path: Path, backend_factory: Callable[[Settings], Backend]
) -> None:
    if not sys.stdin.isatty():
        raise ToolError(
            "INTERACTIVE_CONFIGURATION_REQUIRED", "Run configure in an interactive terminal."
        )
    backend = backend_factory(settings)
    try:
        _, folders = await backend.catalog()
        for folder in folders:
            print(f"{folder.id}: {folder.title}")
        value = (
            await asyncio.to_thread(
                input, "Allowed folder IDs, comma-separated (Enter: keep current): "
            )
        ).strip()
        if value:
            chosen = [int(item.strip()) for item in value.split(",")]
            if set(chosen) - {f.id for f in folders}:
                raise ToolError("FOLDER_NOT_FOUND", "A selected folder does not exist.")
            settings.allowed_folder_ids = chosen
        value = (
            await asyncio.to_thread(
                input, "Additional chat IDs, comma-separated (Enter: keep current): "
            )
        ).strip()
        if value:
            settings.allowed_chat_ids = [str(int(item.strip())) for item in value.split(",")]
        settings.allow_public_channel_reads = await prompt_boolean(
            "Allow explicitly requested public channels outside this scope?",
            settings.allow_public_channel_reads,
        )
        settings.allow_public_channel_discovery = await prompt_boolean(
            "Allow public-channel search and similar-channel metadata (no post reads)?",
            settings.allow_public_channel_discovery,
        )
        settings.allow_all_chats = False
        save_settings(settings, path)
        print(f"Scope saved to {path}. Restart the MCP server to apply configuration changes.")
    finally:
        await backend.close()


async def diagnose(
    settings: Settings, dependencies: CliDependencies, catalog: bool = False
) -> None:
    backend = dependencies.backend_factory(settings)
    try:
        if catalog:
            chats, folders = await backend.catalog()
            output: object = {
                "folders": [f.model_dump(mode="json") for f in folders],
                "chats": [c.model_dump(mode="json") for c in chats],
            }
        else:
            output = await dependencies.service_factory(backend, settings).get_status()
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        await backend.close()


@dataclass(frozen=True)
class CliDependencies:
    backend_factory: Callable[[Settings], Backend]
    service_factory: Callable[[Backend, Settings], Service]
    authenticate: Callable[..., Awaitable[None]]


def main(dependencies: CliDependencies) -> None:
    parser = argparse.ArgumentParser(description="Local read-only Telegram MCP server")
    parser.add_argument("--config", type=Path, help="Configuration TOML path")
    parser.add_argument("command", choices=["serve", "login", "configure", "status", "catalog"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        path = (args.config or config_path()).expanduser()
        settings = load_settings(path)
        if args.command == "serve":
            create_server(
                dependencies.service_factory(dependencies.backend_factory(settings), settings)
            ).run(transport="stdio")
        elif args.command == "login":
            asyncio.run(login(settings, path, dependencies.authenticate))
        elif args.command == "configure":
            asyncio.run(configure(settings, path, dependencies.backend_factory))
        else:
            asyncio.run(diagnose(settings, dependencies, args.command == "catalog"))
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except (ValidationError, ValueError, OSError):
        print(
            "Invalid configuration or local I/O error. "
            "Check the configuration and file permissions.",
            file=sys.stderr,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
