"""Source-linked UTF-8 export parts, preserving whole original message records."""

import json
import re
from datetime import datetime
from urllib.parse import quote, urlsplit

from telegram_mcp.domain.changes import Change, ExportFormat
from telegram_mcp.domain.models import Chat, Issue, Message, MessageRef, ToolError
from telegram_mcp.domain.text import clip


def protected_record(ref: MessageRef, *, pending_batch: bool = False) -> Issue:
    """An explicit omission for a message Telegram does not allow saving."""
    where = " in a pending batch" if pending_batch else ""
    return Issue(
        code="EXPORT_PROTECTED",
        message=f"Telegram restricts saving this message{where}.",
        message_ref=ref,
    )


def omitted_summary(count: int) -> Issue:
    return Issue(code="EXPORT_PROTECTED", message=f"{count} protected records omitted; see export.")


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _fence(text: str, language: str = "text") -> str:
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}\n"


def _label(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", " ".join(text.splitlines()))


def _message_markdown(message: Message, heading: str) -> str:
    ref = message.message_ref.key
    source = f"Source: `{ref}`"
    if message.url:
        url = urlsplit(message.url)
        if (
            url.scheme in ("http", "https")
            and url.hostname in ("t.me", "telegram.me")
            and not url.username
        ):
            source = f"[Original message](<{quote(message.url, safe=':/?=&%#')}>) · `{ref}`"
    lines = [f"## {heading} · {ref}", source, f"Sent: {message.sent_at.isoformat()}"]
    if message.edited_at:
        lines.append(f"Edited: {message.edited_at.isoformat()}")
    if message.sender.name or message.sender.id:
        lines.append(f"Author: {_label(message.sender.name or message.sender.id or '')}")
    lines.extend(["", _fence(message.text)])
    metadata = message.model_dump(
        mode="json",
        exclude_none=True,
        include={
            "entities",
            "reply_to",
            "thread_ref",
            "topic_id",
            "forward_origin",
            "attachments",
            "album_id",
            "web_page",
            "poll",
        },
    )
    metadata = {key: value for key, value in metadata.items() if value != []}
    if metadata:
        lines.extend(
            ["Original metadata:", _fence(json.dumps(metadata, ensure_ascii=False), "json")]
        )
    return "\n\n".join(lines) + "\n\n"


class ExportPart:
    """Reserve footer space so accepting a record never splits it at the byte limit."""

    def __init__(
        self,
        format: ExportFormat,
        chat: Chat,
        *,
        kind: str,
        generated_at: datetime,
        upper_id: int | None,
        date_from: datetime | None,
        date_to: datetime | None,
        max_bytes: int,
    ):
        self.format = format
        self.max_bytes = max_bytes
        self.reserve = min(64 * 1024, max_bytes // 4)
        self.records = 0
        metadata = {
            "record": "manifest",
            "schema_version": 1,
            "kind": kind,
            "source": chat.model_dump(mode="json", exclude_none=True),
            "generated_at": generated_at.isoformat(),
            "upper_message_id": upper_id,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "coverage": "observed_versions",
            "attachments": "metadata_only",
        }
        header = (
            _json(metadata)
            if format == "jsonl"
            else (
                f"# Telegram {kind} export\n\n{_label(chat.title)}\n\n"
                + _fence(json.dumps(metadata, ensure_ascii=False), "json")
                + "\n"
            ).encode("utf-8")
        )
        self.chunks = [header]
        self.size = len(header)
        if self.size + self.reserve > max_bytes:
            raise ToolError("EXPORT_RECORD_TOO_LARGE", "Export metadata exceeds the part limit.")

    def add(self, value: Message | Change | Issue) -> bool:
        if isinstance(value, Message):
            value = clip(value, len(value.text))
            data = {
                "record": "message",
                "message": value.model_dump(mode="json", exclude={"chat"}, exclude_none=True),
            }
            rendered = _message_markdown(value, "Message") if self.format == "markdown" else ""
        elif isinstance(value, Change):
            if value.message is not None:
                value = value.model_copy(
                    update={"message": clip(value.message, len(value.message.text))}
                )
            data = {
                "record": "change",
                **value.model_dump(mode="json", exclude_none=True, exclude={"message": {"chat"}}),
            }
            rendered = (
                _message_markdown(value.message, value.kind)
                if value.message is not None
                else f"## {value.kind} · {value.message_ref.key}\n\n"
            ) + f"Revision: `{value.revision}` · Observed: {value.observed_at.isoformat()}\n\n"
        else:
            data = {"record": "omitted", "issue": value.model_dump(mode="json", exclude_none=True)}
            rendered = (
                "## Omitted record\n\n"
                + _fence(json.dumps(data, ensure_ascii=False), "json")
                + "\n"
            )
        chunk = _json(data) if self.format == "jsonl" else rendered.encode("utf-8")
        if self.size + len(chunk) + self.reserve > self.max_bytes:
            if self.records == 0:
                raise ToolError(
                    "EXPORT_RECORD_TOO_LARGE", "One complete record exceeds the export part limit."
                )
            return False
        self.chunks.append(chunk)
        self.size += len(chunk)
        self.records += 1
        return True

    def finish(self, summary: dict[str, object]) -> bytes:
        data = {"record": "summary", **summary}
        chunk = (
            _json(data)
            if self.format == "jsonl"
            else (
                "## Coverage and continuation\n\n"
                + _fence(json.dumps(data, ensure_ascii=False), "json")
            ).encode("utf-8")
        )
        if self.size + len(chunk) > self.max_bytes:
            raise ToolError("EXPORT_TOO_LARGE", "Export summary exceeds the part limit.")
        return b"".join([*self.chunks, chunk])
