"""Local commands for login, selecting scope, diagnosis and serving MCP."""

import argparse
import asyncio
import fcntl
import getpass
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError
from telethon import TelegramClient

from telegram_mcp.config import Settings, config_path, load_settings
from telegram_mcp.models import ToolError
from telegram_mcp.server import create_server
from telegram_mcp.service import Service
from telegram_mcp.telegram import TelegramBackend


def save_settings(settings: Settings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = settings.model_dump(mode="json", exclude_none=True)
    if settings.api_hash:
        data["api_hash"] = settings.api_hash.get_secret_value()
    text = (
        "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in data.items())
        + "\n"
    )
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def login(settings: Settings, path: Path) -> None:
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
    api_id, api_hash = settings.credentials()
    session = settings.session_path
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(session) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ToolError("SESSION_IN_USE", "Stop the MCP server before logging in.") from None
        previous_umask = os.umask(0o077)
        try:
            client: Any = TelegramClient(
                str(session),
                api_id,
                api_hash,
                device_model="Telegram Context MCP",
                app_version="0.1.0",
            )
            try:
                await client.start(
                    phone=lambda: input("Phone number: "),
                    code_callback=lambda: getpass.getpass("Telegram login code: "),
                    password=lambda: getpass.getpass("Telegram 2FA password: "),
                )
                if (await client.get_me()).bot:
                    raise ToolError("USER_ACCOUNT_REQUIRED", "This server expects a user account.")
            finally:
                await client.disconnect()
        finally:
            os.umask(previous_umask)
        session.chmod(0o600)
        save_settings(settings, path)
        print(f"Session saved. Configuration: {path}")
        print("Next: telegram-mcp configure — choose the folders/chats the server may read.")
    finally:
        os.close(fd)


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


async def configure(settings: Settings, path: Path) -> None:
    if not sys.stdin.isatty():
        raise ToolError(
            "INTERACTIVE_CONFIGURATION_REQUIRED", "Run configure in an interactive terminal."
        )
    backend = TelegramBackend(settings)
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


async def diagnose(settings: Settings, catalog: bool = False) -> None:
    backend = TelegramBackend(settings)
    try:
        if catalog:
            chats, folders = await backend.catalog()
            output: object = {
                "folders": [f.model_dump(mode="json") for f in folders],
                "chats": [c.model_dump(mode="json") for c in chats],
            }
        else:
            output = await Service(backend, settings).get_status()
        print(json.dumps(output, ensure_ascii=False, indent=2))
    finally:
        await backend.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local read-only Telegram MCP server")
    parser.add_argument("--config", type=Path, help="Configuration TOML path")
    parser.add_argument("command", choices=["serve", "login", "configure", "status", "catalog"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        path = (args.config or config_path()).expanduser()
        settings = load_settings(path)
        if args.command == "serve":
            create_server(Service(TelegramBackend(settings), settings)).run(transport="stdio")
        elif args.command == "login":
            asyncio.run(login(settings, path))
        elif args.command == "configure":
            asyncio.run(configure(settings, path))
        else:
            asyncio.run(diagnose(settings, args.command == "catalog"))
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


if __name__ == "__main__":
    main()
