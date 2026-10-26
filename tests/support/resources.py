import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from mcp.client import Client
from mcp_types import (
    BlobResourceContents,
    CallToolResult,
    ResourceLink,
    TextContent,
)

from telegram_mcp.application.service import Service
from telegram_mcp.domain.models import DownloadResult, MessageRef, ToolError

MAX_BYTES = 20 * 1024 * 1024

URI = "telegram-attachment://downloads/synthetic-token"


def synthetic_bytes(size: int) -> bytes:
    """Exercise every byte value rather than compressible text or a tiny fixture."""
    pattern = bytes(range(256))
    return (pattern * ((size + len(pattern) - 1) // len(pattern)))[:size]


class ResourceService:
    """Isolate the public transport seam from Telegram and application storage tests."""

    def __init__(self, directory: Path, size: int = 1024) -> None:
        self.payload = synthetic_bytes(size)
        self.directory = directory
        self.download_calls: list[str] = []
        self.read_calls: list[str] = []
        self.failure: Exception | None = None
        self.closed = False

    def service(self) -> Service:
        return cast(Service, self)

    async def download_attachment(self, attachment_id: str) -> DownloadResult:
        self.download_calls.append(attachment_id)
        if self.failure is not None:
            raise self.failure
        return DownloadResult(
            attachment_id=attachment_id,
            message_ref=MessageRef(chat_id="1", message_id=1),
            name="binary.dat",
            mime_type="application/x-telegram-test",
            size=len(self.payload),
            sha256=hashlib.sha256(self.payload).hexdigest(),
            local_path=str(self.directory / "binary.dat"),
            resource_uri=URI,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    async def read_download_resource(self, uri: str) -> tuple[bytes, str]:
        self.read_calls.append(uri)
        if self.failure is not None:
            raise self.failure
        if uri != URI:
            raise ToolError("DOWNLOAD_UNAVAILABLE", "The download is unknown or expired.")
        return self.payload, "application/x-telegram-test"

    async def close(self) -> None:
        self.closed = True


def assert_compact_download(result: CallToolResult, size: int) -> str:
    assert not result.is_error
    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["size"] == size
    assert payload["message_ref"] == "1:1"
    assert payload["sha256"] == hashlib.sha256(synthetic_bytes(size)).hexdigest()
    assert len(result.content) == 2
    text, link = result.content
    assert isinstance(text, TextContent)
    assert text.text == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert isinstance(link, ResourceLink)
    assert link.uri == payload["resource_uri"] == URI
    assert link.mime_type == payload["mime_type"] == "application/x-telegram-test"
    assert link.size == size
    assert len(result.model_dump_json()) < 4096
    assert "blob" not in payload
    return str(link.uri)


async def assert_binary_read(client: Client, uri: str, size: int) -> None:
    result = await client.read_resource(uri)
    assert len(result.contents) == 1
    resource = result.contents[0]
    assert isinstance(resource, BlobResourceContents)
    assert resource.uri == uri
    assert resource.mime_type == "application/x-telegram-test"
    assert base64.b64decode(resource.blob, validate=True) == synthetic_bytes(size)
