"""Local commands for login, selecting scope, diagnosis and serving MCP."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr, ValidationError

from telegram_mcp.adapters.config import (
    Settings,
    config_path,
    load_settings,
    save_settings,
    validate_settings,
    validation_error,
)
from telegram_mcp.adapters.mcp import create_server
from telegram_mcp.adapters.version import get_version
from telegram_mcp.application.ports import Backend
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import Chat, Folder, ToolError


async def login(
    settings: Settings, path: Path, authenticate: Callable[..., Awaitable[None]]
) -> None:
    if not sys.stdin.isatty():
        raise ToolError(
            "INTERACTIVE_LOGIN_REQUIRED", "Run login yourself in an interactive terminal."
        )
    settings = settings.model_copy(deep=True)
    if settings.api_id is None:
        value = (await asyncio.to_thread(input, "Telegram api_id (my.telegram.org): ")).strip()
        try:
            settings.api_id = int(value)
        except ValueError:
            raise ToolError("INVALID_ARGUMENT", "api_id: enter a positive integer.") from None
    if settings.api_hash is None:
        settings.api_hash = SecretStr(getpass.getpass("Telegram api_hash: ").strip())
    settings = validate_settings(settings, "Login input")
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


async def load_catalog(
    backend: Backend, *, timeout_seconds: float = 300
) -> tuple[list[Chat], list[Folder]]:
    """Keep incremental progress alive while a local command waits for its catalog."""
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                try:
                    return await backend.catalog()
                except ToolError as exc:
                    if exc.code != "CATALOG_LOADING":
                        raise
                    print(exc.detail, file=sys.stderr)
                    await asyncio.sleep(exc.retry_after or 1)
    except TimeoutError:
        raise ToolError(
            "CATALOG_TIMEOUT",
            "Catalog loading exceeded the local command's time limit. "
            "Check the Telegram connection and try again; use the running MCP server "
            "to keep incremental progress between requests.",
        ) from None


async def configure(
    settings: Settings, path: Path, backend_factory: Callable[[Settings], Backend]
) -> None:
    if not sys.stdin.isatty():
        raise ToolError(
            "INTERACTIVE_CONFIGURATION_REQUIRED", "Run configure in an interactive terminal."
        )
    updated = settings.model_copy(deep=True)
    backend = backend_factory(settings)
    try:
        _, folders = await load_catalog(backend)
        for folder in folders:
            print(f"{folder.id}: {folder.title}")
        value = (
            await asyncio.to_thread(
                input, "Allowed folder IDs, comma-separated (Enter: keep current): "
            )
        ).strip()
        if value:
            try:
                chosen = [int(item.strip()) for item in value.split(",")]
            except ValueError:
                raise ToolError(
                    "INVALID_ARGUMENT", "allowed_folder_ids: enter comma-separated integer IDs."
                ) from None
            if set(chosen) - {f.id for f in folders}:
                raise ToolError("FOLDER_NOT_FOUND", "A selected folder does not exist.")
            updated.allowed_folder_ids = chosen
        value = (
            await asyncio.to_thread(
                input, "Additional chat IDs, comma-separated (Enter: keep current): "
            )
        ).strip()
        if value:
            try:
                updated.allowed_chat_ids = [str(int(item.strip())) for item in value.split(",")]
            except ValueError:
                raise ToolError(
                    "INVALID_ARGUMENT", "allowed_chat_ids: enter comma-separated integer IDs."
                ) from None
        updated.allow_public_channel_reads = await prompt_boolean(
            "Allow explicitly requested public channels outside this scope?",
            settings.allow_public_channel_reads,
        )
        updated.allow_public_channel_discovery = await prompt_boolean(
            "Allow public-channel search and similar-channel metadata (no post reads)?",
            settings.allow_public_channel_discovery,
        )
        updated.allow_all_chats = await prompt_boolean(
            "Allow reading all account chats instead of limiting reads to selected sources?",
            settings.allow_all_chats,
        )
        updated.allow_attachment_downloads = await prompt_boolean(
            "Allow selected attachment downloads from permitted sources (up to 20 MiB each)?",
            settings.allow_attachment_downloads,
        )
        save_settings(validate_settings(updated, "Configuration input"), path)
        print(f"Scope saved to {path}. Restart the MCP server to apply configuration changes.")
    finally:
        await backend.close()


async def diagnose(
    settings: Settings, dependencies: CliDependencies, catalog: bool = False
) -> None:
    backend = dependencies.backend_factory(settings)
    try:
        if catalog:
            chats, folders = await load_catalog(backend)
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
    serve_http: Callable[[Service, Settings], None]
    connect: Callable[[Settings], Coroutine[object, object, None]]


def main(dependencies: CliDependencies) -> None:
    parser = argparse.ArgumentParser(
        prog="telegram-mcp", description="Local read-only Telegram MCP server"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {get_version()}")
    parser.add_argument("--config", type=Path, help="Configuration TOML path")
    parser.add_argument(
        "command", choices=["serve", "connect", "login", "configure", "status", "catalog"]
    )
    parser.add_argument(
        "--transport", choices=["stdio", "http"], help="Transport for serve (default: stdio)"
    )
    args = parser.parse_args()
    if args.transport is not None and args.command != "serve":
        parser.error("--transport is supported only with serve")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        path = (args.config or config_path()).expanduser()
        settings = load_settings(path)
        if args.command == "serve":
            service = dependencies.service_factory(dependencies.backend_factory(settings), settings)
            if args.transport == "http":
                dependencies.serve_http(service, settings)
            else:
                create_server(service).run(transport="stdio")
        elif args.command == "connect":
            asyncio.run(dependencies.connect(settings))
        elif args.command == "login":
            asyncio.run(login(settings, path, dependencies.authenticate))
        elif args.command == "configure":
            asyncio.run(configure(settings, path, dependencies.backend_factory))
        else:
            asyncio.run(diagnose(settings, dependencies, args.command == "catalog"))
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        if exc.code == "SESSION_IN_USE":
            print(
                "A running server owns this Telegram session. Use get_status through a connected "
                "MCP app, or stop the server before running direct local commands.",
                file=sys.stderr,
            )
        sys.exit(1)
    except ValidationError as exc:
        print(str(validation_error(exc, "Local command")), file=sys.stderr)
        sys.exit(1)
    except OSError:
        print(
            "LOCAL_IO_ERROR: Local command failed to access a file or device. "
            "Check paths and permissions.",
            file=sys.stderr,
        )
        sys.exit(1)
    except EOFError:
        print(
            "INPUT_CLOSED: Interactive input ended; configuration was not saved.", file=sys.stderr
        )
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
