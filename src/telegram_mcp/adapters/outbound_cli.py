"""Local maintenance preserves deduplication receipts and never contacts Telegram."""

import argparse
import asyncio
import json

from telegram_mcp.adapters.config import Settings
from telegram_mcp.adapters.outbound_store import OutboundStore
from telegram_mcp.adapters.private_paths import prepare_private_directory
from telegram_mcp.adapters.session import SessionLease


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    choices = {"files": {"list", "remove"}, "operations": {"list", "prune-payloads"}}
    if args.checkpoint_action not in choices[args.command]:
        parser.error(f"Use {args.command} {' or '.join(sorted(choices[args.command]))}")
    if args.chat_id is not None or args.all or args.include_protected:
        parser.error("Checkpoint selectors are not supported by this command")
    if args.checkpoint_action == "remove" and not args.file_id:
        parser.error("files remove requires --file-id")
    if args.file_id and args.checkpoint_action != "remove":
        parser.error("--file-id requires files remove")
    if args.checkpoint_action == "list" and args.apply:
        parser.error("--apply requires a cleanup action")


def run(settings: Settings, args: argparse.Namespace) -> None:
    prepare_private_directory(settings.session_path.parent)
    lease = SessionLease(settings.session_path)
    lease.acquire()
    try:
        asyncio.run(_run(settings, args))
    finally:
        lease.close()


async def _run(settings: Settings, args: argparse.Namespace) -> None:
    store = OutboundStore(settings.session_path)
    try:
        if args.command == "files":
            if args.checkpoint_action == "remove":
                if args.apply:
                    await store.remove_file(args.file_id)
                result = {"file_id": args.file_id, "applied": args.apply}
            else:
                result = {
                    "files": [file.model_dump(mode="json") for file in await store.all_files()]
                }
        elif args.checkpoint_action == "prune-payloads":
            result = {
                "applied": args.apply,
                "pruned": await store.prune_payloads() if args.apply else 0,
                "deduplication_records_preserved": True,
            }
        else:
            result = {"operations": await store.journal()}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await store.close()
