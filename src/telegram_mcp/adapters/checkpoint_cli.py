"""Offline inspection and explicit cleanup under exclusive session ownership."""

import argparse
import json
import re

from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.private_paths import prepare_private_directory
from telegram_mcp.adapters.session import SessionLease
from telegram_mcp.domain.models import ToolError


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("checkpoint_action", nargs="?", choices=["status", "prune"])
    parser.add_argument("--chat-id", help="Limit checkpoint cleanup to one canonical chat ID")
    parser.add_argument(
        "--all", action="store_true", help="Explicitly select all checkpoint sources"
    )
    parser.add_argument(
        "--include-protected",
        action="store_true",
        help="Include active positions; invalidates their tokens",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Delete selected checkpoints instead of previewing"
    )


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    flags = args.chat_id is not None or args.all or args.include_protected or args.apply
    if args.command != "checkpoints":
        if args.checkpoint_action is not None or flags:
            parser.error("Checkpoint arguments require the checkpoints command")
    elif args.checkpoint_action is None:
        parser.error("Use checkpoints status or checkpoints prune")
    elif args.checkpoint_action == "status" and flags:
        parser.error("Cleanup options require checkpoints prune")
    elif args.chat_id is not None and args.all:
        parser.error("Use --chat-id or --all, not both")
    elif args.include_protected and args.chat_id is None and not args.all:
        parser.error(
            "--include-protected requires --chat-id or --all; active positions will be lost"
        )


def run(settings: Settings, args: argparse.Namespace) -> None:
    if args.chat_id is not None and not re.fullmatch(r"-?[1-9]\d*", args.chat_id):
        raise ToolError("INVALID_ARGUMENT", "--chat-id: use a canonical numeric chat ID.")
    prepare_private_directory(settings.session_path.parent)
    lease = SessionLease(settings.session_path)
    lease.acquire()
    store = CheckpointStore(settings.session_path, SystemClock())
    try:
        result = (
            store.status()
            if args.checkpoint_action == "status"
            else store.prune(
                chat_id=args.chat_id, include_protected=args.include_protected, apply=args.apply
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()
        lease.close()
