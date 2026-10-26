"""Opt-in, selected-source live checks with bounded reads and redacted output."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Never, Protocol
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    # Keep the documented direct-script invocation working alongside python -m.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.probe_artifacts import check_attachment, check_changes_and_exports
from scripts.probe_monitoring import check_monitoring
from telegram_mcp.adapters.archive import ArchiveStore
from telegram_mcp.adapters.checkpoints import CheckpointStore
from telegram_mcp.adapters.clock import SystemClock
from telegram_mcp.adapters.config import ConfiguredPolicyProvider, Settings, load_settings
from telegram_mcp.adapters.cursors import CursorStore
from telegram_mcp.adapters.downloads import DownloadStore
from telegram_mcp.adapters.exports import ExportStore
from telegram_mcp.adapters.telegram import TelegramBackend
from telegram_mcp.adapters.watches import WatchStore
from telegram_mcp.application.attachment_ports import (
    AttachmentKind,
    AttachmentSource,
    AttachmentWriter,
)
from telegram_mcp.application.attachments import parse_attachment_id
from telegram_mcp.application.ports import Backend, ExecutionLimits, MessageOffset
from telegram_mcp.application.requests import MessageRead
from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import Chat, ContentType, MessageRef, ToolError

PAGE_SIZE = 5
MAX_SELECTORS = 3
MAX_PAGES = 2
MAX_SECONDS = 180
KNOWN_CODES = frozenset(
    {
        "ACCESS_DENIED",
        "ALBUM_SCAN_INCOMPLETE",
        "AUTH_REQUIRED",
        "CONNECTION_ERROR",
        "CURSOR_EXPIRED",
        "CURSOR_MISMATCH",
        "FORUM_UNAVAILABLE",
        "INVALID_ARGUMENT",
        "INVALID_CONFIGURATION",
        "INVALID_CURSOR",
        "INVALID_REFERENCE",
        "LOCAL_IO_ERROR",
        "MESSAGE_UNAVAILABLE",
        "NOT_AN_ALBUM",
        "NOT_FOUND",
        "PAGINATION_INCOMPLETE",
        "PAGINATION_STALLED",
        "QUERY_TOO_LARGE",
        "RATE_LIMITED",
        "SEARCH_INEXACT",
        "SESSION_ERROR",
        "SESSION_IN_USE",
        "SESSION_LOCK_ERROR",
        "TELEGRAM_ERROR",
        "TELEGRAM_UNAVAILABLE",
        "TOPIC_UNAVAILABLE",
        "VALIDATION_SCOPE_DENIED",
        "VALIDATION_TIMEOUT",
        "UNEXPECTED_ERROR",
        "INTERRUPTED",
        "ATTACHMENT_UNAVAILABLE",
        "ATTACHMENT_PROTECTED",
        "ATTACHMENT_TOO_LARGE",
        "DOWNLOADS_DISABLED",
        "DOWNLOAD_UNAVAILABLE",
        "DOWNLOAD_STORAGE_FULL",
        "CHECKPOINT_EXPIRED",
        "CHECKPOINT_INCOMPATIBLE",
        "CHECKPOINT_STORAGE_ERROR",
        "CHECKPOINT_STORAGE_FULL",
        "EXPORT_PROTECTED",
        "EXPORT_UNAVAILABLE",
        "EXPORT_STORAGE_FULL",
        "EXPORT_RECORD_TOO_LARGE",
        "ARCHIVE_STORAGE_ERROR",
        "WATCH_NOT_FOUND",
        "WATCH_CONFLICT",
        "WATCH_STORAGE_FULL",
        "WATCH_STORAGE_ERROR",
        "WATCH_BATCH_MISMATCH",
        "VALIDATION_MISMATCH",
    }
)
STOP_CODES = frozenset(
    {
        "AUTH_REQUIRED",
        "CONNECTION_ERROR",
        "RATE_LIMITED",
        "SESSION_IN_USE",
        "SESSION_LOCK_ERROR",
        "TELEGRAM_UNAVAILABLE",
        "VALIDATION_SCOPE_DENIED",
    }
)


def safe_code(value: object) -> str:
    return value if isinstance(value, str) and value in KNOWN_CODES else "UNEXPECTED_ERROR"


@dataclass
class Check:
    check: str
    count: int = 0
    issue_codes: list[str] = field(default_factory=list)
    status: str = "passed"

    def __post_init__(self) -> None:
        if self.issue_codes:
            self.status = "failed"


class ProbeBackend(Backend, Protocol):
    def select(
        self, chat_ids: set[str], *, attachment_id: str | None = None, m4: bool = False
    ) -> None: ...
    async def disconnect_owned(self) -> None: ...


class LiveBackend(TelegramBackend):
    """Fail closed if a future application change broadens a probe's scope."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.selected: set[str] = set()
        self.stopped: ToolError | None = None
        self.attachment_id: str | None = None
        self.m4 = False

    def select(
        self, chat_ids: set[str], *, attachment_id: str | None = None, m4: bool = False
    ) -> None:
        self.selected = set(chat_ids)
        self.attachment_id, self.m4 = attachment_id, m4

    def require_incremental(self, chat_id: str, count: int = 1) -> None:
        self.require_selected(chat_id)
        if not self.m4 or not 0 <= count <= 100:
            raise ToolError("VALIDATION_SCOPE_DENIED", "Incremental read is outside probe bounds.")

    async def recent_observations(
        self, chat_id: str, limit: int, *, before: datetime | None = None
    ) -> Any:
        self.require_incremental(chat_id, limit)
        return await self.bounded_read(super().recent_observations(chat_id, limit, before=before))

    async def peer_states(self, chat_ids: list[str]) -> Any:
        if not 1 <= len(chat_ids) <= 40:
            raise ToolError(
                "VALIDATION_SCOPE_DENIED", "Select at most 40 peers for metadata checks."
            )
        for cid in chat_ids:
            self.require_incremental(cid)
        return await self.bounded_read(super().peer_states(chat_ids))

    async def observe_messages(self, chat_id: str, message_ids: list[int]) -> Any:
        self.require_incremental(chat_id, len(message_ids))
        return await self.bounded_read(super().observe_messages(chat_id, message_ids))

    async def read_incremental(
        self,
        chat_id: str,
        *,
        after_id: int,
        through_id: int,
        limit: int,
        date_from: datetime | None = None,
    ) -> Any:
        self.require_incremental(chat_id, limit)
        return await self.bounded_read(
            super().read_incremental(
                chat_id,
                after_id=after_id,
                through_id=through_id,
                limit=min(limit, PAGE_SIZE),
                date_from=date_from,
            )
        )

    async def check_export_source(self, chat_id: str) -> None:
        self.require_incremental(chat_id)
        await self.bounded_read(super().check_export_source(chat_id))

    def require_attachment(self, attachment_id: str, chat_id: str) -> None:
        self.require_selected(chat_id)
        if self.attachment_id is None or attachment_id != self.attachment_id:
            raise ToolError(
                "VALIDATION_SCOPE_DENIED", "Attachment was not selected for this probe."
            )

    async def prepare_attachment(self, ref: MessageRef, kind: AttachmentKind) -> AttachmentSource:
        self.require_attachment(f"{ref.key}:{kind}", ref.chat_id)
        return await self.bounded_read(super().prepare_attachment(ref, kind))

    async def download_attachment(self, source: AttachmentSource, writer: AttachmentWriter) -> None:
        self.require_attachment(source.attachment_id, source.message_ref.chat_id)
        await self.bounded_read(super().download_attachment(source, writer))

    def require_selected(self, chat_id: str, limit: int = 1) -> None:
        if self.stopped is not None:
            raise self.stopped
        if chat_id not in self.selected or not 0 <= limit <= PAGE_SIZE:
            raise ToolError("VALIDATION_SCOPE_DENIED", "Read is outside the probe bounds.")

    async def bounded_read(self, operation: Awaitable[Any]) -> Any:
        try:
            return await operation
        except ToolError as exc:
            # A service may collect several directional issues. Do not issue another RPC
            # after a transport failure, even before that service call returns its result.
            if exc.code in STOP_CODES:
                self.stopped = exc
            raise

    async def catalog(self) -> Any:
        raise ToolError("VALIDATION_SCOPE_DENIED", "Catalog reads are excluded from this probe.")

    async def search_global(self, *args: Any, **kwargs: Any) -> Any:
        raise ToolError("VALIDATION_SCOPE_DENIED", "Global search is excluded from this probe.")

    async def search_public_channels(self, *args: Any, **kwargs: Any) -> Any:
        raise ToolError("VALIDATION_SCOPE_DENIED", "Discovery is excluded from this probe.")

    async def get_similar_channels(self, *args: Any, **kwargs: Any) -> Any:
        raise ToolError("VALIDATION_SCOPE_DENIED", "Discovery is excluded from this probe.")

    async def discussion_chat(self, chat: Chat) -> str:
        raise ToolError(
            "VALIDATION_SCOPE_DENIED", "Linked discussions are excluded from this probe."
        )

    async def thread_root(self, ref: MessageRef, discussion_chat_id: str) -> MessageRef:
        raise ToolError(
            "VALIDATION_SCOPE_DENIED", "Linked discussions are excluded from this probe."
        )

    async def read(
        self, chat_id: str, request: MessageRead, *, offset: MessageOffset = 0, limit: int = 30
    ) -> Any:
        self.require_selected(chat_id, limit)
        return await self.bounded_read(super().read(chat_id, request, offset=offset, limit=limit))

    async def message(self, ref: MessageRef) -> Any:
        self.require_selected(ref.chat_id)
        return await self.bounded_read(super().message(ref))

    async def messages(self, chat_id: str, message_ids: list[int]) -> Any:
        self.require_selected(chat_id, len(message_ids))
        return await self.bounded_read(super().messages(chat_id, message_ids))

    async def history_window(self, chat_id: str, *, anchor_id: int, limit: int, newer: bool) -> Any:
        self.require_selected(chat_id, limit)
        return await self.bounded_read(
            super().history_window(chat_id, anchor_id=anchor_id, limit=limit, newer=newer)
        )

    async def fetch_topics(self, chat_id: str, **kwargs: Any) -> Any:
        self.require_selected(chat_id, kwargs.get("limit", 30))
        return await self.bounded_read(super().fetch_topics(chat_id, **kwargs))

    async def get_topic(self, chat_id: str, topic_id: int) -> Any:
        self.require_selected(chat_id)
        return await self.bounded_read(super().get_topic(chat_id, topic_id))

    async def disconnect_owned(self) -> None:
        client = self.runtime.client
        if client is None or not self.runtime.lease.held:
            raise ToolError("VALIDATION_SCOPE_DENIED", "The probe does not own a connected client.")
        # Preserve this runtime and its lease so the next call exercises reconnect.
        await self.runtime.call(client.disconnect())


class StopProbe(Exception):
    pass


class Recorder:
    def __init__(self, emit: Callable[[Check], None]):
        self.emit = emit
        self.checks: list[Check] = []

    def add(
        self, name: str, count: int = 0, codes: list[str] | None = None, *, skipped: bool = False
    ) -> None:
        check = Check(name, count, sorted({safe_code(code) for code in codes or []}))
        if skipped:
            check.status = "skipped"
        self.checks.append(check)
        self.emit(check)
        if STOP_CODES.intersection(check.issue_codes):
            raise StopProbe

    async def call(self, name: str, operation: Any) -> Any:
        try:
            result = await operation
        except ToolError as exc:
            self.add(name, codes=[exc.code])
            return None
        issues = getattr(result, "issues", [])
        if isinstance(result, dict):
            authorized = result.get("authorized") is True
            error = result.get("error")
            code = error.get("code") if isinstance(error, dict) else "AUTH_REQUIRED"
            self.add(name, int(authorized), [] if authorized else [safe_code(code)])
        else:
            messages = getattr(result, "messages", None)
            topics = getattr(result, "topics", None)
            if isinstance(result, int):
                count = result
            elif hasattr(result, "returned_count"):
                count = result.returned_count
            elif hasattr(result, "exported_count"):
                count = result.exported_count
            elif messages is not None:
                count = len(messages)
            elif topics is not None:
                count = len(topics)
            else:
                count = int(getattr(result, "anchor", None) is not None)
                count += sum(len(getattr(result, field, [])) for field in ("before", "after"))
            self.add(name, count, [issue.code for issue in issues])
        return result


def validate_selectors(
    chats: list[str], refs: list[str], query: str, *, monitoring: bool = False
) -> None:
    maximum = 40 if monitoring else MAX_SELECTORS
    if not 1 <= len(chats) <= maximum or len(refs) > MAX_SELECTORS:
        raise ToolError(
            "INVALID_ARGUMENT", f"Select one to {maximum} chats and at most three messages."
        )
    if any(not value.strip() for value in [*chats, *refs]) or not 1 <= len(query.strip()) <= 64:
        raise ToolError(
            "INVALID_ARGUMENT", "Selectors must be nonempty; query must be 1–64 characters."
        )
    try:
        if any("comment" in parse_qs(urlparse(ref).query) for ref in refs):
            raise ToolError("VALIDATION_SCOPE_DENIED", "Linked discussion references are excluded.")
    except ValueError:
        raise ToolError("INVALID_ARGUMENT", "Invalid message selector.") from None


async def probe(
    settings: Settings,
    *,
    chats: list[str],
    message_refs: list[str],
    query: str = "link",
    topics: bool = False,
    attachment_id: str | None = None,
    m4: bool = False,
    monitoring: bool = False,
    backend_factory: Callable[[Settings], ProbeBackend] = LiveBackend,
    emit: Callable[[Check], None] = lambda _: None,
) -> list[Check]:
    """Run only selected reads; temporary probe data is removed on every exit path."""
    validate_selectors(chats, message_refs, query, monitoring=monitoring)
    if monitoring and (message_refs or topics or attachment_id or m4):
        raise ToolError("INVALID_ARGUMENT", "--monitoring is a separate metadata-only probe.")
    restricted = settings.model_copy(
        deep=True,
        update={
            "allowed_chat_ids": [],
            "allowed_folder_ids": [],
            "allow_all_chats": False,
            "allow_public_channel_reads": False,
            "allow_public_channel_discovery": False,
            "rpc_pages_per_call": 1,
        },
    )
    backend = backend_factory(restricted)
    recorder = Recorder(emit)
    temporary = TemporaryDirectory(prefix="telegram-mcp-probe-")
    root = Path(temporary.name)
    service: Service | None = None
    try:
        async with asyncio.timeout(MAX_SECONDS):
            selected: dict[str, Chat] = {}
            for ref in chats:
                chat = await backend.resolve_chat(ref, refresh=True)
                selected[chat.id] = chat
            resolved = [await backend.resolve_message(ref) for ref in message_refs]
            if any(ref.chat_id not in selected for ref in resolved):
                raise ToolError(
                    "VALIDATION_SCOPE_DENIED", "Message source must be selected explicitly."
                )
            restricted.allowed_chat_ids = list(selected)
            if attachment_id is not None:
                attachment_ref, _ = parse_attachment_id(attachment_id)
                if attachment_ref.chat_id not in selected:
                    raise ToolError(
                        "VALIDATION_SCOPE_DENIED", "Attachment source must be selected explicitly."
                    )
            backend.select(set(selected), attachment_id=attachment_id, m4=m4 or monitoring)
            recorder.add("selected_sources", len(selected))
            clock = SystemClock()
            service = Service(
                backend,
                ConfiguredPolicyProvider(restricted),
                ExecutionLimits(
                    rpc_pages_per_call=1, page_size=PAGE_SIZE, text_limit=256, text_budget=1280
                ),
                CursorStore(clock, maximum=32, max_bytes=1024 * 1024),
                clock,
                DownloadStore(restricted.session_path, clock, root=root / "downloads"),
                CheckpointStore(restricted.session_path, clock, root=root / "checkpoints"),
                ExportStore(restricted.session_path, clock, root=root / "exports"),
                WatchStore(restricted.session_path, root=root / "watches"),
                ArchiveStore(root / "archive", clock),
            )
            await recorder.call("status", service.get_status())
            if monitoring:
                await check_monitoring(service, backend, list(selected), recorder)
                return recorder.checks
            for index, chat in enumerate(selected.values()):
                cursor = None
                for number in range(MAX_PAGES):
                    result = await recorder.call(
                        f"history_{index}_{number}",
                        service.get_history(
                            chat_id=chat.id,
                            limit=PAGE_SIZE,
                            cursor=cursor,
                        ),
                    )
                    cursor = result.next_cursor if result is not None else None
                    if cursor is None:
                        break
                await recorder.call(
                    f"keyword_{index}",
                    service.search_messages(
                        query=query.strip(),
                        chat_ids=[chat.id],
                        limit=PAGE_SIZE,
                    ),
                )
                cursor = None
                for number in range(MAX_PAGES):
                    result = await recorder.call(
                        f"links_{index}_{number}",
                        service.search_messages(
                            query="",
                            has_link=True,
                            chat_ids=[chat.id],
                            limit=PAGE_SIZE,
                            cursor=cursor,
                        ),
                    )
                    cursor = result.next_cursor if result is not None else None
                    if cursor is None:
                        break
                for content in ("photo", "voice", "audio"):
                    kind: ContentType = content
                    await recorder.call(
                        f"{content}_{index}",
                        service.search_messages(
                            query=query.strip(),
                            content_type=kind,
                            chat_ids=[chat.id],
                            limit=PAGE_SIZE,
                        ),
                    )
                if topics and chat.is_forum:
                    listed = await recorder.call(
                        f"topics_{index}", service.list_topics(chat.id, limit=3)
                    )
                    if listed is not None and listed.topics:
                        await recorder.call(
                            f"topic_history_{index}",
                            service.get_history(
                                chat_id=chat.id,
                                topic_id=listed.topics[0].id,
                                limit=PAGE_SIZE,
                            ),
                        )
            if resolved:
                result = await recorder.call(
                    "messages", service.get_messages(list(resolved), text_limit=256)
                )
                available = (
                    {message.message_ref.key: message for message in result.messages}
                    if result is not None
                    else {}
                )
                for index, ref in enumerate(resolved):
                    if ref.key not in available:
                        continue
                    await recorder.call(
                        f"context_{index}",
                        service.get_context(ref, before=2, after=2, reply_depth=0),
                    )
                    if available[ref.key].album_id is not None:
                        await recorder.call(f"album_{index}", service.get_album(ref, scan_limit=5))
            if attachment_id is not None:
                await recorder.call(
                    "attachment_resource_and_archive", check_attachment(service, attachment_id)
                )
            else:
                recorder.add("attachment_resource_and_archive", skipped=True)
            if m4:
                await check_changes_and_exports(
                    service,
                    recorder,
                    list(selected),
                    restricted.session_path,
                    root / "checkpoints",
                    clock,
                    await backend.checkpoint_binding(),
                )
            else:
                recorder.add("changes_and_exports", skipped=True)
            await backend.disconnect_owned()
            recorder.add("disconnect_owned", 1)
            await recorder.call("reconnect_status", service.get_status())
            await recorder.call(
                "reconnect_history",
                service.get_history(chat_id=next(iter(selected)), limit=PAGE_SIZE),
            )
    except StopProbe:
        pass
    except TimeoutError:
        recorder.add("probe", codes=["VALIDATION_TIMEOUT"])
    except ToolError as exc:
        try:
            recorder.add("probe", codes=[exc.code])
        except StopProbe:
            pass
    except Exception:
        recorder.add("probe", codes=["UNEXPECTED_ERROR"])
    finally:
        try:
            if service is not None:
                await service.close()
            else:
                await backend.close()
        except Exception:
            recorder.add("close", codes=["UNEXPECTED_ERROR"])
        finally:
            temporary.cleanup()
    return recorder.checks


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ToolError("INVALID_ARGUMENT", "Invalid validation arguments; see --help.")


def parser() -> argparse.ArgumentParser:
    result = Parser(description=__doc__)
    result.add_argument("--config", type=Path, help="Local config path; never modified.")
    result.add_argument(
        "--chat",
        action="append",
        required=True,
        help="Explicit chat ID, username or link; up to three references, or 40 with --monitoring.",
    )
    result.add_argument(
        "--message-ref",
        action="append",
        default=[],
        help="Message in a selected chat; repeat up to three times.",
    )
    result.add_argument(
        "--query", default="link", help="Selected-source keyword, 1–64 characters (default: link)."
    )
    result.add_argument(
        "--topics",
        action="store_true",
        help="Also check one page of topics in selected forum chats.",
    )
    result.add_argument(
        "--attachment-id",
        help="Download and verify exactly this selected attachment (up to 20 MiB).",
    )
    result.add_argument(
        "--m4",
        action="store_true",
        help="Check durable changes and two bounded one-day export parts per selected source.",
    )
    result.add_argument(
        "--monitoring",
        action="store_true",
        help="Separate metadata-only check: up to 40 selected --chat references, "
        "two peer batches and bounded watch creation.",
    )
    return result


def print_check(check: Check) -> None:
    print(json.dumps(asdict(check), separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    # Third-party diagnostics may include peer names; this dedicated process emits only checks.
    logging.disable(logging.CRITICAL)
    try:
        args = parser().parse_args(argv)
        validate_selectors(args.chat, args.message_ref, args.query, monitoring=args.monitoring)
        checks = asyncio.run(
            probe(
                load_settings(args.config),
                chats=args.chat,
                message_refs=args.message_ref,
                query=args.query,
                topics=args.topics,
                attachment_id=args.attachment_id,
                m4=args.m4,
                monitoring=args.monitoring,
                emit=print_check,
            )
        )
        return int(any(check.issue_codes for check in checks))
    except ToolError as exc:
        print_check(Check("setup", issue_codes=[safe_code(exc.code)]))
        return 1
    except KeyboardInterrupt:
        print_check(Check("probe", issue_codes=["INTERRUPTED"]))
        return 130
    except Exception:
        print_check(Check("setup", issue_codes=["UNEXPECTED_ERROR"]))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
